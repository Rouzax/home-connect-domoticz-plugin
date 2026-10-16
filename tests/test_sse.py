import pytest

import sse


def run(lines):
    p = sse.Parser()
    return [e for e in (p.feed(line) for line in lines) if e is not None]


def test_basic_event_like_the_live_capture():
    events = run(
        [
            b"event:STATUS\n",
            b'data:{"haId":"H","items":[{"key":"K","value":1}]}\n',
            b"id:H\n",
            b"\n",
        ]
    )
    assert events == [sse.Event("STATUS", '{"haId":"H","items":[{"key":"K","value":1}]}', "H")]
    assert sse.items(events[0]) == [{"key": "K", "value": 1}]


def test_keep_alive_and_crlf_and_space_after_colon():
    events = run([b"event: KEEP-ALIVE\r\n", b"data:\r\n", b"\r\n"])
    assert events == [sse.Event("KEEP-ALIVE", "", "")]


def test_multiline_data_and_comments():
    events = run([b": comment\n", b"event:NOTIFY\n", b"data:a\n", b"data:b\n", b"\n"])
    assert events[0].data == "a\nb"


def test_only_the_first_colon_separates_field_and_value():
    events = run([b"event:STATUS\n", b"data:a:b\n", b"\n"])
    assert events[0].data == "a:b"


def test_blank_line_without_fields_dispatches_nothing():
    assert run([b"\n", b"\n"]) == []


def test_items_tolerates_bad_json():
    assert sse.items(sse.Event("STATUS", "not json", "")) == []
    assert sse.items(sse.Event("STATUS", '{"items": "x"}', "")) == []


def test_items_skips_non_object_entries():
    event = sse.Event("STATUS", '{"items": [1, {"key": "K"}, "x"]}', "")
    assert sse.items(event) == [{"key": "K"}]


def test_oversized_line_raises():
    p = sse.Parser()
    with pytest.raises(sse.Overflow):
        p.feed(b"data:" + b"x" * sse.MAX_LINE)


def test_line_at_the_limit_is_accepted():
    p = sse.Parser()
    assert p.feed(b"data:" + b"x" * (sse.MAX_LINE - 5)) is None


def test_oversized_event_raises():
    p = sse.Parser()
    with pytest.raises(sse.Overflow):
        for _ in range(3):
            p.feed(b"data:" + b"x" * (sse.MAX_EVENT // 2) + b"\n")


def test_many_small_data_lines_hit_the_event_cap():
    # Each line is far under MAX_LINE; only the accumulated event size can stop the growth.
    p = sse.Parser()
    accepted = 0
    with pytest.raises(sse.Overflow):
        for _ in range(sse.MAX_EVENT * 2):
            p.feed(b"data:x\n")
            accepted += 1
    assert accepted <= sse.MAX_EVENT


def test_comment_flood_never_overflows():
    p = sse.Parser()
    for _ in range(100_000):
        assert p.feed(b": ping\n") is None


def test_parser_is_clean_after_overflow():
    p = sse.Parser()
    p.feed(b"event:STALE\n")
    p.feed(b"data:" + b"x" * (sse.MAX_EVENT - 10) + b"\n")
    with pytest.raises(sse.Overflow):
        p.feed(b"data:" + b"x" * 100 + b"\n")
    assert p.feed(b"event:STATUS\n") is None
    assert p.feed(b"data:{}\n") is None
    assert p.feed(b"\n") == sse.Event("STATUS", "{}", "")


def test_undecodable_utf8_does_not_raise():
    events = run([b"event:STATUS\n", b"data:\xff\xfe\n", b"\n"])
    assert events == [sse.Event("STATUS", "��", "")]


def test_items_on_undecodable_json_returns_empty():
    assert sse.items(sse.Event("STATUS", "��", "")) == []


def test_deeply_nested_event_data_does_not_escape():
    deep = "[" * 200_000
    assert sse.items(sse.Event("STATUS", deep, "")) == []


def test_deep_nesting_within_the_event_cap_does_not_escape():
    # Fits under MAX_EVENT, so it reaches the decoder and must fail inside parse_json.
    lines = [b"event:STATUS\n", b"data:" + b"[" * 60_000 + b"\n", b"\n"]
    events = run(lines)
    assert events and sse.items(events[0]) == []
