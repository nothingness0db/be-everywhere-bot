from db.accounts import Account
from apis.types import MediaItem, OutboundPost, Post
from config import (
    ATTRIBUTION_TEMPLATES,
    NETWORK_BILIBILI,
    NETWORK_INSTAGRAM,
    NETWORK_LINKEDIN,
    NETWORK_LIMITS,
    NETWORK_RSS,
    NETWORK_TELEGRAM,
    NETWORK_TWITTER,
    SOURCE_ONLY_NETWORKS,
)
from sync.engine import (
    _attribution_text,
    _dest_id_per_source_post,
    _destination_accounts,
    _filter_original_posts,
    _group_by_conversation,
    _reserve_attribution,
    _slice_media_bytes,
    _with_attribution,
)
from utils.thread_processor import build_outbound_posts


def test_source_only_networks():
    assert NETWORK_RSS in SOURCE_ONLY_NETWORKS
    assert NETWORK_INSTAGRAM in SOURCE_ONLY_NETWORKS
    assert NETWORK_TWITTER not in SOURCE_ONLY_NETWORKS
    assert NETWORK_LINKEDIN not in SOURCE_ONLY_NETWORKS


def test_group_by_conversation(post_factory, utc_now):
    root = post_factory("1", conversation_id="thread")
    reply = post_factory(
        "2",
        conversation_id="thread",
        in_reply_to_id="1",
    )
    groups = _group_by_conversation([reply, root])
    assert len(groups) == 1
    assert [p.id for p in groups[0]] == ["1", "2"]


def test_filter_original_posts_skips_mirrored(post_factory):
    posts = [post_factory("1"), post_factory("2")]
    filtered = _filter_original_posts(posts, {"2"})
    assert [p.id for p in filtered] == ["1"]


def test_filter_original_posts_no_mirrored(post_factory):
    posts = [post_factory("1")]
    assert _filter_original_posts(posts, set()) == posts


def test_destination_accounts_excludes_self_and_source_only(post_factory):
    source = Account(id=1, network=NETWORK_TWITTER, label="default", remote_id="1")
    dest = Account(id=2, network="telegram", label="default", remote_id="2")
    rss = Account(id=3, network=NETWORK_RSS, label="blog", remote_id="3")
    instagram = Account(id=4, network=NETWORK_INSTAGRAM, label="main", remote_id="4")
    linkedin = Account(id=5, network=NETWORK_LINKEDIN, label="main", remote_id="5")
    result = _destination_accounts(
        source, [source, dest, rss, instagram, linkedin]
    )
    assert result == [dest, linkedin]


def test_slice_media_bytes():
    media_a = MediaItem(url="a", media_type="photo")
    media_b = MediaItem(url="b", media_type="photo")
    outbounds = [
        OutboundPost(text="", media=[media_a, media_b], source_post_ids=["1"]),
        OutboundPost(text="tail", media=[], source_post_ids=["1"]),
    ]
    all_bytes = [b"a", b"b"]
    assert _slice_media_bytes(outbounds, all_bytes) == [[b"a", b"b"], []]


def test_dest_id_per_source_post_maps_merged_thread():
    outbounds = [
        OutboundPost(text="merged", media=[], source_post_ids=["a", "b", "c"]),
    ]
    mapping = _dest_id_per_source_post(outbounds, ["dest-1"])
    assert mapping == {"a": "dest-1", "b": "dest-1", "c": "dest-1"}


def test_dest_id_per_source_post_multiple_outbounds():
    outbounds = [
        OutboundPost(text="1", media=[], source_post_ids=["a"]),
        OutboundPost(text="2", media=[], source_post_ids=["b"]),
    ]
    mapping = _dest_id_per_source_post(outbounds, ["dest-1", "dest-2"])
    assert mapping == {"a": "dest-1", "b": "dest-2"}


def test_attribution_names_the_source_network():
    assert _attribution_text(NETWORK_BILIBILI, NETWORK_TELEGRAM, "@news") == "转自 Tg"


def test_attribution_is_absent_for_unconfigured_destinations():
    assert _attribution_text(NETWORK_TELEGRAM, NETWORK_TWITTER, "@someone") == ""


def test_attribution_template_can_include_the_source_account(monkeypatch):
    monkeypatch.setitem(ATTRIBUTION_TEMPLATES, "bilibili", "来自 {source}")
    assert (
        _attribution_text(NETWORK_BILIBILI, NETWORK_TELEGRAM, "Eira 的频道")
        == "来自 Eira 的频道"
    )


def test_attribution_goes_on_the_first_message_only():
    outbounds = [
        OutboundPost(text="正文", media=[], source_post_ids=["1"]),
        OutboundPost(text="续篇", media=[], source_post_ids=["1"]),
    ]
    _with_attribution(outbounds, "转自 Telegram")
    assert [o.text for o in outbounds] == ["转自 Telegram\n\n正文", "续篇"]


def test_attribution_marks_a_media_only_post(photo):
    outbounds = [OutboundPost(text="", media=[photo], source_post_ids=["1"])]
    assert _with_attribution(outbounds, "转自 Telegram")[0].text == "转自 Telegram"


def test_attribution_reserves_room_in_the_text_limits():
    limits = NETWORK_LIMITS[NETWORK_BILIBILI]
    line = "转自 Telegram"
    reserved = _reserve_attribution(limits, line)
    assert reserved.max_text == limits.max_text - len(line) - 2
    assert reserved.max_caption == limits.max_caption - len(line) - 2


def test_attribution_never_overflows_the_destination_limit(post_factory):
    limits = NETWORK_LIMITS[NETWORK_BILIBILI]
    line = "转自 Telegram"
    outbounds = build_outbound_posts(
        [post_factory(text="中" * 2500)], _reserve_attribution(limits, line)
    )
    _with_attribution(outbounds, line)
    assert outbounds[0].text.startswith(line + "\n\n")
    assert all(len(o.text) <= limits.max_text for o in outbounds)
