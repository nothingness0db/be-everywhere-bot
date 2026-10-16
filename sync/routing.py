"""Explicit opt-in routing for Telegram channel posts."""

import math
from dataclasses import dataclass, field, replace
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


def parse_prefixes(text: str, config: RoutingConfig) -> tuple[set[str], str]:
    """Consume only complete leading tokens, so 'xavier' never selects X."""
    remaining = text.lstrip()
    targets: set[str] = set()
    while remaining:
        parts = remaining.split(maxsplit=1)
        token = parts[0].casefold()
        if token not in config.prefixes:
            break
        targets.update(config.prefixes[token])
        remaining = parts[1] if len(parts) == 2 else ""
    return targets, remaining if targets else text


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
