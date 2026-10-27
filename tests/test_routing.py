from dataclasses import replace

import pytest

from db.accounts import Account
from sync.routing import RoutingConfig, load_routing, parse_prefixes, route_post

DUAL_ROUTING = RoutingConfig(prefixes={"x": ("twitter",), "b": ("bilibili",)})


@pytest.mark.parametrize(
    "text,targets,body",
    [
        ("普通消息", set(), "普通消息"),
        ("正文 #x", {"twitter"}, "正文"),
        ("#b 正文", {"bilibili"}, "正文"),
        ("正文 #x #b", {"twitter", "bilibili"}, "正文"),
        ("正文 #B\n#x", {"twitter", "bilibili"}, "正文"),
        ("正文 #x 都发 #b", {"twitter", "bilibili"}, "正文 都发"),
        ("#xavier test", set(), "#xavier test"),
        ("#x正文", set(), "#x正文"),
        ("正文 #battle", set(), "正文 #battle"),
        ("正文#x", set(), "正文#x"),
        ("正文 x", set(), "正文 x"),
        ("#x", {"twitter"}, ""),
        ("正文 #x #x", {"twitter"}, "正文"),
    ],
)
def test_parse_prefixes(text, targets, body):
    assert parse_prefixes(text, DUAL_ROUTING) == (targets, body)


def test_route_preserves_source_and_other_targets(post_factory):
    source = Account(1, "telegram", "main", "@channel")
    dest = Account(2, "twitter", "main", "user")
    post = post_factory(text="正文 #x #b")
    assert route_post(post, source, dest, DUAL_ROUTING).text == "正文"
    assert (
        route_post(post, source, replace(dest, network="bilibili"), DUAL_ROUTING).text
        == "正文"
    )
    assert (
        route_post(post, source, replace(dest, network="mastodon"), DUAL_ROUTING)
        is None
    )
    assert route_post(post, dest, source, DUAL_ROUTING) is None
    assert post.text == "正文 #x #b"


def test_custom_prefix_can_target_one_account(post_factory):
    config = RoutingConfig(prefixes={"work": ("twitter/work",)})
    source = Account(1, "telegram", "main", "@channel")
    work = Account(2, "twitter", "work", "a")
    post = post_factory(text="hello #work")
    assert route_post(post, source, work, config).text == "hello"
    assert route_post(post, source, replace(work, label="personal"), config) is None


def test_prefix_only_requires_media(post_factory, photo):
    source = Account(1, "telegram", "main", "@channel")
    dest = Account(2, "twitter", "main", "a")
    assert route_post(post_factory(text="#x"), source, dest, DUAL_ROUTING) is None
    assert (
        route_post(
            post_factory(text="#x", media=[photo]), source, dest, DUAL_ROUTING
        ).text
        == ""
    )


@pytest.mark.parametrize(
    "text",
    [
        'mode = "typo"',
        "poll_interval_seconds = 0",
        "album_settle_seconds = -1",
        "poll_interval_seconds = nan",
        "poll_interval_seconds = true",
        '[prefixes]\nx = ["typo"]',
        '[prefixes]\nx = ["rss"]',
        '[prefixes]\nx = ["telegram"]',
        '[prefixes]\nx = ["twitter/"]',
        '[prefixes]\nx = "twitter"',
        "typo = 1",
        '[prefixes]\n"x b" = ["twitter"]',
    ],
)
def test_invalid_configuration_fails_closed(tmp_path, text):
    path = tmp_path / "routing.toml"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_routing(path)


def test_load_config(tmp_path):
    path = tmp_path / "routing.toml"
    path.write_text(
        'poll_interval_seconds = 3\n[prefixes]\nall = ["twitter/work", "bilibili"]'
    )
    config = load_routing(path)
    assert config.poll_interval_seconds == 3
    assert parse_prefixes("text #all", config) == (
        {"twitter/work", "bilibili"},
        "text",
    )


def test_default_routing_only_publishes_to_bilibili():
    assert parse_prefixes("x text", RoutingConfig()) == (set(), "x text")
    assert parse_prefixes("text #b", RoutingConfig()) == ({"bilibili"}, "text")
