import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import ModuleType

from sqlalchemy.engine import Engine

import apis.bilibili as bilibili
import apis.bluesky as bluesky
import apis.instagram as instagram
import apis.linkedin as linkedin
import apis.mastodon as mastodon
import apis.rss as rss
import apis.telegram as telegram
import apis.threads as threads
import apis.twitter as twitter
from apis.types import OutboundPost, Post
from config import (
    BACKFILL_POST_DELAY_SECONDS,
    DESTINATION_ONLY_NETWORKS,
    NETWORK_BILIBILI,
    NETWORK_BLUESKY,
    NETWORK_INSTAGRAM,
    NETWORK_LINKEDIN,
    NETWORK_MASTODON,
    NETWORK_RSS,
    NETWORK_TELEGRAM,
    NETWORK_THREADS,
    NETWORK_TWITTER,
    POST_MIN_AGE_MINUTES,
    REPLY_FILTER_NETWORKS,
    SOURCE_ONLY_NETWORKS,
    TWITTER_FETCH_MAX_PAGES,
    WATCH_INITIAL_LOOKBACK_HOURS,
    WATCH_OVERLAP_HOURS,
)
from db.accounts import Account, account_display_name, get_account, list_accounts
from db.deliveries import delivery_key, get_delivery, save_delivery
from db.health import credential_error_status, safe_error, set_health
from db.jobs import deserialize_posts, ensure_job, finish_job, get_job, start_job
from db.sync_state import (
    get_dest_post_id,
    get_last_synced_at,
    get_mirrored_post_ids,
    is_synced,
    mark_synced,
    record_mirrored_post,
    set_last_synced_at,
)
from sync.routing import RoutingConfig, route_post
from utils.filters import exclude_source_only_posts, filter_own_threads
from utils.posts import sort_chronologically
from utils.thread_processor import (
    build_outbound_posts,
    collect_ready_batch,
    get_network_limits,
)
from utils.urls import unwrap_posts_text

logger = logging.getLogger(__name__)

_NETWORKS: dict[str, ModuleType] = {
    NETWORK_BILIBILI: bilibili,
    NETWORK_TWITTER: twitter,
    NETWORK_TELEGRAM: telegram,
    NETWORK_MASTODON: mastodon,
    NETWORK_THREADS: threads,
    NETWORK_BLUESKY: bluesky,
    NETWORK_RSS: rss,
    NETWORK_INSTAGRAM: instagram,
    NETWORK_LINKEDIN: linkedin,
}


def _api(network: str) -> ModuleType:
    try:
        return _NETWORKS[network]
    except KeyError as exc:
        raise ValueError(f"Unknown network: {network}") from exc


def _fetch_since(
    engine: Engine,
    account: Account,
    since: datetime | None,
) -> datetime | None:
    if since is not None:
        return since
    now = datetime.now(timezone.utc)
    last_sync = get_last_synced_at(engine, account.id)
    if last_sync:
        return last_sync - timedelta(hours=WATCH_OVERLAP_HOURS)
    return now - timedelta(hours=WATCH_INITIAL_LOOKBACK_HOURS)


def _group_by_conversation(posts: list[Post]) -> list[list[Post]]:
    groups: dict[str, list[Post]] = defaultdict(list)
    for post in posts:
        groups[post.conversation_id].append(post)
    return [sort_chronologically(group) for group in groups.values()]


def _filter_original_posts(posts: list[Post], mirrored_ids: set[str]) -> list[Post]:
    if not mirrored_ids:
        return posts
    filtered = [p for p in posts if p.id not in mirrored_ids]
    skipped = len(posts) - len(filtered)
    if skipped:
        logger.info("Skipped %d mirrored post(s) created by sync", skipped)
    return filtered


def _destination_accounts(
    source: Account, all_accounts: list[Account]
) -> list[Account]:
    return [
        account
        for account in all_accounts
        if account.id != source.id and account.network not in SOURCE_ONLY_NETWORKS
    ]


async def _download_media_flat(
    engine: Engine, source: Account, posts: list[Post]
) -> list[bytes]:
    api = _api(source.network)
    return [
        await api.download_media(item, engine, source.id)
        for post in posts
        for item in post.media
    ]


def _slice_media_bytes(
    outbounds: list[OutboundPost], all_bytes: list[bytes]
) -> list[list[bytes]]:
    offset = 0
    sliced: list[list[bytes]] = []
    for outbound in outbounds:
        n = len(outbound.media)
        sliced.append(all_bytes[offset : offset + n])
        offset += n
    return sliced


def _dest_id_per_source_post(
    outbounds: list[OutboundPost], dest_ids: list[str]
) -> dict[str, str]:
    """Map each source post id to the last dest id published for it."""
    mapping: dict[str, str] = {}
    for outbound, dest_id in zip(outbounds, dest_ids):
        for source_id in outbound.source_post_ids:
            mapping[source_id] = dest_id
    return mapping


async def _chain_anchor(
    engine: Engine,
    source: Account,
    dest: Account,
    thread: list[Post],
    batch: list[Post],
) -> str | None:
    """Reply ref when this batch continues a partially synced conversation."""
    if not batch:
        return None

    ordered = sort_chronologically(thread)
    first_id = batch[0].id
    idx = next((i for i, post in enumerate(ordered) if post.id == first_id), -1)
    if idx <= 0:
        return None

    prev_post = ordered[idx - 1]
    if not is_synced(engine, source.id, prev_post.id, dest.id):
        return None

    dest_post_id = get_dest_post_id(engine, source.id, prev_post.id, dest.id)
    if not dest_post_id:
        return None

    if dest.network == NETWORK_BLUESKY:
        return await bluesky.resolve_reply_target(engine, dest.id, dest_post_id)

    return dest_post_id


async def _publish_batch(
    engine: Engine,
    source: Account,
    dest: Account,
    batch: list[Post],
    thread: list[Post],
    post_delay_seconds: float,
) -> tuple[list[str], list[OutboundPost]]:
    """Publish one batch to a destination account. Returns created post IDs."""
    api = _api(dest.network)
    limits = get_network_limits(dest.network)
    if dest.network == NETWORK_BILIBILI and any(
        item.media_type != "photo" for post in batch for item in post.media
    ):
        raise ValueError(
            "Bilibili supports text/photo dynamics only; this post contains video/GIF"
        )
    await unwrap_posts_text(batch)

    if source.network == NETWORK_INSTAGRAM:
        outbounds = instagram.build_outbounds(batch, limits)
    elif dest.network == NETWORK_TWITTER:
        outbounds = twitter.build_outbounds(batch, limits)
    else:
        outbounds = build_outbound_posts(batch, limits)
    if len(outbounds) > 1:
        logger.info(
            "[%s -> %s] Publishing %d message(s) in order",
            account_display_name(source, engine),
            account_display_name(dest, engine),
            len(outbounds),
        )

    reply_to = await _chain_anchor(engine, source, dest, thread, batch)

    dest_ids: list[str] = []
    downloaded: dict[tuple[str, str], bytes] = {}
    for i, outbound in enumerate(outbounds):
        key = delivery_key(outbound, i)
        result = get_delivery(engine, source.id, dest.id, key)
        if result is None:
            bytes_chunk = []
            if dest.network != NETWORK_THREADS:
                for item in outbound.media:
                    media_key = (item.url, item.media_type)
                    if media_key not in downloaded:
                        downloaded[media_key] = await _api(
                            source.network
                        ).download_media(item, engine, source.id)
                    bytes_chunk.append(downloaded[media_key])
            result = await api.publish_outbound(
                engine,
                dest.id,
                outbound,
                bytes_chunk,
                reply_to=reply_to,
            )
            save_delivery(engine, source.id, dest.id, key, result)
            record_mirrored_post(engine, dest.id, result.post_id)
        dest_ids.append(result.post_id)
        reply_to = result.reply_ref
        if post_delay_seconds > 0 and i < len(outbounds) - 1:
            await asyncio.sleep(post_delay_seconds)
    return dest_ids, outbounds


def _record_success(
    engine: Engine,
    source: Account,
    dest: Account,
    batch: list[Post],
    outbounds: list[OutboundPost],
    dest_ids: list[str],
) -> int:
    """Record sync mappings and mirrored posts. Returns count of new mappings."""
    if not dest_ids:
        return 0

    per_post_dest = _dest_id_per_source_post(outbounds, dest_ids)
    recorded = 0
    for post in batch:
        dest_post_id = per_post_dest.get(post.id, dest_ids[-1])
        if mark_synced(
            engine,
            source_account_id=source.id,
            source_post_id=post.id,
            dest_account_id=dest.id,
            dest_post_id=dest_post_id,
        ):
            recorded += 1

    for dest_id in dest_ids:
        record_mirrored_post(engine, dest.id, dest_id)

    return recorded


async def publish_tracked(
    engine: Engine,
    source: Account,
    dest: Account,
    batch: list[Post],
    thread: list[Post],
    post_delay_seconds: float = 0,
) -> tuple[int, list[str]]:
    job = ensure_job(engine, source.id, dest.id, batch)
    start_job(engine, job["id"], batch)
    try:
        dest_ids, outbounds = await _publish_batch(
            engine, source, dest, batch, thread, post_delay_seconds
        )
        recorded = _record_success(engine, source, dest, batch, outbounds, dest_ids)
    except asyncio.CancelledError:
        finish_job(engine, job["id"], error="发送已中断，可重试继续。")
        raise
    except Exception as exc:
        detail = safe_error(engine, exc, source.id, dest.id)
        finish_job(engine, job["id"], error=detail)
        if detail.startswith("Bilibili session/CSRF expired"):
            set_health(
                engine,
                dest.id,
                "expired",
                "B 站 Cookie 或 CSRF 已失效，请在服务器重新配置账号。",
            )
        elif detail.startswith("X "):
            set_health(engine, dest.id, credential_error_status(detail), detail)
        elif detail.startswith("Telegram "):
            set_health(engine, source.id, credential_error_status(detail), detail)
        raise
    finish_job(engine, job["id"], dest_ids=dest_ids)
    set_health(engine, dest.id, "healthy", "最近一次发布成功，登录状态与发布权限可用。")
    return recorded, dest_ids


async def retry_job(engine: Engine, job_id: int) -> int:
    job = get_job(engine, job_id)
    if job is None or job["status"] != "queued":
        return 0
    source = get_account(engine, job["source_account_id"])
    dest = get_account(engine, job["dest_account_id"])
    if source is None or dest is None:
        finish_job(engine, job_id, error="来源或目标账号已不存在。")
        return 0
    posts = deserialize_posts(job["payload"])
    try:
        count, _ = await publish_tracked(engine, source, dest, posts, posts)
        return count
    except Exception:
        logger.exception("Manual retry failed for job %d", job_id)
        return 0


async def sync_account(
    engine: Engine,
    source: Account,
    all_accounts: list[Account],
    since: datetime | None = None,
    enforce_min_age: bool = True,
    min_age_minutes: int = POST_MIN_AGE_MINUTES,
    post_delay_seconds: float = 0,
    routing: RoutingConfig | None = None,
) -> int:
    routing = routing or RoutingConfig()
    if source.network in DESTINATION_ONLY_NETWORKS:
        return 0
    if routing.mode == "telegram_prefix" and source.network != NETWORK_TELEGRAM:
        return 0
    dest_accounts = _destination_accounts(source, all_accounts)
    if routing.mode == "telegram_prefix":
        dest_accounts = [a for a in dest_accounts if a.network != NETWORK_TELEGRAM]
    if not dest_accounts:
        logger.info(
            "[%s] No publishable accounts configured — nothing to sync",
            account_display_name(source, engine),
        )
        return 0

    fetch_since = _fetch_since(engine, source, since)
    # Telegram history is in our durable inbox, including failures older than the overlap.
    if source.network == NETWORK_TELEGRAM:
        fetch_since = since
    max_pages = (
        None
        if since is not None or source.network in (NETWORK_RSS, NETWORK_INSTAGRAM)
        else TWITTER_FETCH_MAX_PAGES
    )
    source_name = account_display_name(source, engine)

    if max_pages and source.network not in (NETWORK_RSS, NETWORK_INSTAGRAM):
        logger.info(
            "[%s] Fetching posts since %s (max %d API page(s))",
            source_name,
            fetch_since.strftime("%Y-%m-%d %H:%M UTC") if fetch_since else "all",
            max_pages,
        )
    elif since is not None:
        logger.info(
            "[%s] Backfill: fetching posts since %s",
            source_name,
            fetch_since.strftime("%Y-%m-%d %H:%M UTC") if fetch_since else "all",
        )
    else:
        logger.info(
            "[%s] Fetching posts since %s",
            source_name,
            fetch_since.strftime("%Y-%m-%d %H:%M UTC") if fetch_since else "all",
        )

    try:
        fetch_options = {}
        if source.network == NETWORK_TELEGRAM:
            fetch_options["album_settle_seconds"] = routing.album_settle_seconds
        posts = await _api(source.network).fetch_posts(
            engine,
            source.id,
            since=fetch_since,
            include_replies=True,
            max_pages=max_pages,
            **fetch_options,
        )
    except Exception as exc:
        detail = safe_error(engine, exc, source.id)
        set_health(engine, source.id, credential_error_status(detail), detail)
        logger.exception(
            "[%s] Fetch failed — skipping until next cycle",
            source_name,
        )
        return 0
    if source.network == NETWORK_TELEGRAM:
        set_health(engine, source.id, "healthy", "最近一次频道消息收取成功。")
    mirrored_ids = get_mirrored_post_ids(engine, source.id)
    posts = _filter_original_posts(posts, mirrored_ids)
    if source.network in REPLY_FILTER_NETWORKS:
        posts = filter_own_threads(posts)
    posts = exclude_source_only_posts(posts)

    pending = sum(
        1
        for p in posts
        if any(
            route_post(p, source, dest, routing) is not None
            and not is_synced(engine, source.id, p.id, dest.id)
            for dest in dest_accounts
        )
    )
    logger.info(
        "[%s] Eligible: %d post(s), pending sync to at least one account: %d",
        source_name,
        len(posts),
        pending,
    )

    if pending == 0:
        set_last_synced_at(engine, source.id, datetime.now(timezone.utc))
        return 0

    synced = 0

    for thread in _group_by_conversation(posts):
        for dest in dest_accounts:
            dest_name = account_display_name(dest, engine)
            routed_thread = [
                routed
                for post in thread
                if (routed := route_post(post, source, dest, routing)) is not None
            ]
            batch = collect_ready_batch(
                routed_thread,
                is_synced=lambda pid, d=dest: is_synced(engine, source.id, pid, d.id),
                enforce_min_age=enforce_min_age and routing.mode == "mesh",
                min_age_minutes=min_age_minutes,
            )
            if not batch:
                continue

            post_ids = ", ".join(p.id for p in batch)
            try:
                recorded, dest_ids = await publish_tracked(
                    engine, source, dest, batch, routed_thread, post_delay_seconds
                )
            except Exception:
                logger.exception(
                    "[%s -> %s] Publish failed for post(s) %s — will retry later",
                    source_name,
                    dest_name,
                    post_ids,
                )
                if post_delay_seconds > 0:
                    await asyncio.sleep(post_delay_seconds)
                continue

            synced += recorded
            logger.info(
                "[%s -> %s] Synced post(s) %s -> %s",
                source_name,
                dest_name,
                post_ids,
                ", ".join(dest_ids),
            )

            if post_delay_seconds > 0:
                await asyncio.sleep(post_delay_seconds)

    set_last_synced_at(engine, source.id, datetime.now(timezone.utc))
    return synced


async def run_sync(
    engine: Engine,
    since: datetime | None = None,
    enforce_min_age: bool = True,
    routing: RoutingConfig | None = None,
) -> int:
    routing = routing or RoutingConfig()
    accounts = list_accounts(engine)
    if not accounts:
        logger.warning("No accounts configured. Run --auth for at least one network.")
        return 0

    # Delay only for explicit --since backfills, not when min-age is off
    # on the first watch cycle after start/restart.
    post_delay = BACKFILL_POST_DELAY_SECONDS if since is not None else 0
    if post_delay:
        logger.info("Backfill mode: %.1fs delay between posts", post_delay)

    total = 0
    for source in accounts:
        source_name = account_display_name(source, engine)
        try:
            total += await sync_account(
                engine,
                source,
                accounts,
                since=since,
                enforce_min_age=enforce_min_age,
                post_delay_seconds=post_delay,
                routing=routing,
            )
        except Exception:
            logger.exception(
                "[%s] Sync failed — continuing with other accounts",
                source_name,
            )
    return total
