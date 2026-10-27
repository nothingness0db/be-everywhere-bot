"""Explicit opt-in routing for Telegram channel posts."""

import math
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

import tomllib

from apis.types import Post
from config import BASE_DIR, NETWORK_TELEGRAM, NETWORKS, SOURCE_ONLY_NETWORKS
from db.accounts import Account


@dataclass(frozen=True)
class RoutingConfig:
    mode: str = "telegram_prefix"
    poll_interval_seconds: float = 5
    album_settle_seconds: float = 2
    prefixes: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {"b": ("bilibili",)}
    )


def load_routing(path: Path | None = None) -> RoutingConfig:
    path = path or BASE_DIR / "routing.toml"
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    return parse_routing(data)


def parse_routing(data: dict) -> RoutingConfig:
    unknown = data.keys() - {
        "mode",
        "poll_interval_seconds",
        "album_settle_seconds",
        "prefixes",
    }
    if unknown:
        raise ValueError(f"Unknown routing settings: {', '.join(sorted(unknown))}")
    mode = data.get("mode", "telegram_prefix")
    if mode not in {"telegram_prefix", "mesh"}:
        raise ValueError("mode must be telegram_prefix or mesh")
    timing = {}
    for key, default, minimum in (
        ("poll_interval_seconds", 5, 1),
        ("album_settle_seconds", 2, 0),
    ):
        value = data.get(key, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < minimum
        ):
            raise ValueError(f"{key} must be a finite number >= {minimum}")
        timing[key] = value
    prefixes = data.get("prefixes", {"b": ["bilibili"]})
    if not isinstance(prefixes, dict) or not prefixes:
        raise ValueError("prefixes must be a nonempty table")
    normalized = {}
    for prefix, targets in prefixes.items():
        if not prefix or len(prefix.split()) != 1 or prefix.strip() != prefix:
            raise ValueError("Each prefix must be a single nonempty word")
        if prefix.casefold() in normalized:
            raise ValueError(f"Duplicate prefix: {prefix}")
        if (
            not isinstance(targets, list)
            or not targets
            or not all(isinstance(t, str) for t in targets)
        ):
            raise ValueError(f"Prefix {prefix!r} requires a nonempty array of targets")
        for target in targets:
            network, separator, label = target.partition("/")
            if (
                network not in NETWORKS
                or network in SOURCE_ONLY_NETWORKS
                or network == NETWORK_TELEGRAM
            ):
                raise ValueError(f"Invalid destination: {target}")
            if separator and (not label.strip() or "/" in label):
                raise ValueError(f"Invalid account label in target: {target}")
        normalized[prefix.casefold()] = tuple(targets)
    return RoutingConfig(mode=mode, prefixes=normalized, **timing)


@lru_cache(maxsize=None)
def _marker_pattern(prefix: str) -> re.Pattern[str]:
    """Match '#prefix' as a whole tag, preceded by a space or the message start."""
    return re.compile(
        rf"(?:(?<=\s)|^)#{re.escape(prefix)}(?=$|\s|[^\w#])",
        re.IGNORECASE,
    )


def parse_prefixes(text: str, config: RoutingConfig) -> tuple[set[str], str]:
    """Collect '#tag' routing markers anywhere in the text and strip them.

    '正文 #b' routes to b's targets and publishes '正文'. The marker must be a
    complete tag — a space (or message start) before it, and no word characters
    right after — so '#battle' never selects b.
    """
    targets: set[str] = set()
    body = text
    for prefix, prefix_targets in config.prefixes.items():
        pattern = _marker_pattern(prefix)
        if pattern.search(body):
            targets.update(prefix_targets)
            body = pattern.sub("", body)
    if not targets:
        return set(), text
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"[ \t]{2,}", " ", body)
    return targets, body.strip()


def route_post(
    post: Post, source: Account, dest: Account, config: RoutingConfig
) -> Post | None:
    if config.mode == "mesh":
        return post
    if source.network != NETWORK_TELEGRAM or dest.network == NETWORK_TELEGRAM:
        return None
    targets, text = parse_prefixes(post.text, config)
    if dest.network not in targets and f"{dest.network}/{dest.label}" not in targets:
        return None
    if not text.strip() and not post.media:
        return None
    return replace(post, text=text)
