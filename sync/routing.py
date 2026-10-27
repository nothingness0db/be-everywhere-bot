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
    # Telegram channel mirroring: source account label -> destination labels.
    # Every post in the source channel is copied to these accounts unchanged.
    forward: dict[str, tuple[str, ...]] = field(default_factory=dict)


def load_routing(path: Path | None = None) -> RoutingConfig:
    path = path or BASE_DIR / "routing.toml"
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    return parse_routing(data)


def _normalize_labels(values, error: str) -> tuple[str, ...]:
    if not isinstance(values, list) or not values:
        raise ValueError(error)
    normalized = []
    for value in values:
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ValueError(error)
        if any(char.isspace() for char in value):
            raise ValueError(error)
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise ValueError(error)
    return tuple(normalized)


def parse_routing(data: dict) -> RoutingConfig:
    unknown = data.keys() - {
        "mode",
        "poll_interval_seconds",
        "album_settle_seconds",
        "prefixes",
        "forward",
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
    forward = data.get("forward", {})
    if not isinstance(forward, dict):
        raise ValueError("forward must be a table of source account labels")
    normalized_forward = {}
    for source_label, targets in forward.items():
        if (
            not isinstance(source_label, str)
            or not source_label
            or source_label.strip() != source_label
            or any(char.isspace() for char in source_label)
        ):
            raise ValueError(
                "Each forward source must be a single nonempty account label"
            )
        normalized_forward[source_label] = _normalize_labels(
            targets,
            f"Forward source {source_label!r} requires a nonempty array of account labels",
        )
    return RoutingConfig(
        mode=mode, prefixes=normalized, forward=normalized_forward, **timing
    )


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
    if source.network != NETWORK_TELEGRAM:
        return None
    if dest.network == NETWORK_TELEGRAM:
        # Channel mirroring keeps the original text (prefixes included) so the
        # destination channel is a faithful copy of the source.
        if dest.label in config.forward.get(source.label, ()):
            return post
        return None
    targets, text = parse_prefixes(post.text, config)
    if dest.network not in targets and f"{dest.network}/{dest.label}" not in targets:
        return None
    if not text.strip() and not post.media:
        return None
    return replace(post, text=text)
