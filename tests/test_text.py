import text

HOSTILE = '<img src=x onerror="alert(1)">\nError: needs re-authorization|level;x'


def test_escape_neutralises_markup():
    assert text.escape(HOSTILE) == (
        "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;\nError: needs re-authorization|level;x"
    )


def test_clean_label_strips_control_and_separator_characters_and_caps_length():
    out = text.clean_label(HOSTILE)
    assert "\n" not in out
    assert "|" not in out and ";" not in out and "<" not in out and ">" not in out
    assert len(out) <= 60
    assert text.clean_label("  Washer   upstairs ") == "Washer upstairs"
    assert text.clean_label("x" * 100, max_len=10) == "x" * 10


def test_clean_label_does_not_leave_trailing_whitespace_after_truncation():
    assert text.clean_label("a b", max_len=2) == "a"


def test_clean_label_non_positive_max_len_is_empty():
    assert text.clean_label("abc", max_len=0) == ""
    assert text.clean_label("abc", max_len=-3) == ""


def test_clean_label_strips_bidi_and_line_separator_characters():
    out = text.clean_label("a‮b\x85c d e")
    assert out == "a b c d e"


def test_log_value_is_one_quoted_line():
    out = text.log_value('a"b\nc')
    assert out.startswith('"') and out.endswith('"')
    assert "\n" not in out
    assert '\\"' in out


def test_log_value_strips_bidi_and_c1_characters():
    out = text.log_value("a‮b\x85c")
    assert "‮" not in out and "\x85" not in out
    assert out == '"a b c"'


def test_ids_and_keys():
    assert text.valid_ha_id("SIEMENS-HCFIXTURE-000000000000")
    assert text.valid_ha_id("012345678901234567")
    assert not text.valid_ha_id("a/b")
    assert not text.valid_ha_id("../x")
    assert not text.valid_ha_id("")
    assert text.valid_key("Cooking.Oven.Option.SetpointTemperature")
    assert text.valid_key("LaundryCare.Washer.EnumType.SpinSpeed.RPM800")
    assert not text.valid_key("a?b")
    assert not text.valid_key("a/b")
    assert not text.valid_key("..")
    assert not text.valid_key(".")


def test_ids_and_keys_reject_trailing_newline():
    assert not text.valid_ha_id("abc\n")
    assert not text.valid_ha_id("SIEMENS-HCFIXTURE-000000000000\n")
    assert not text.valid_key("a.b\n")
    assert not text.valid_key("a.b\n\n")


def test_ids_and_keys_reject_non_string_json_values():
    for bad in (None, 0, 42, 1.5, True, ["x"], {"key": "x"}):
        assert text.valid_ha_id(bad) is False
        assert text.valid_key(bad) is False


def test_auth_link_only_accepts_https_home_connect():
    good = "https://api.home-connect.com/security/oauth/device_verify?user_code=ABCD-EFGH"
    assert text.auth_link(good) == good
    assert text.auth_link("http://api.home-connect.com/x") is None
    assert text.auth_link("https://evil.example/x") is None
    assert text.auth_link("https://home-connect.com.evil.example/x") is None
    assert text.auth_link("https://user@api.home-connect.com/x") is None
    assert text.auth_link('https://api.home-connect.com/"><script>') is None
    assert text.auth_link(None) is None


def test_auth_link_rejects_backslash_host_confusion():
    assert text.auth_link("https://evil.com\\.home-connect.com/x") is None
    assert text.auth_link("https://api.home-connect.com\\@evil.example/x") is None


def test_auth_link_rejects_malformed_brackets_without_raising():
    assert text.auth_link("https://[x/") is None
    assert text.auth_link("https://[::1/x") is None


def test_auth_link_rejects_trailing_newline_and_whitespace():
    assert text.auth_link("https://api.home-connect.com/x\n") is None
    assert text.auth_link("https://api.home-connect.com/x y") is None


def test_auth_link_caps_length_at_512():
    base = "https://api.home-connect.com/"
    at_cap = base + "a" * (512 - len(base))
    assert len(at_cap) == 512
    assert text.auth_link(at_cap) == at_cap
    assert text.auth_link(at_cap + "a") is None


def test_auth_link_requires_dns_character_hostname():
    assert text.auth_link("https://my_host.home-connect.com/x") is None
    assert text.auth_link("https://a~b.home-connect.com/x") is None
