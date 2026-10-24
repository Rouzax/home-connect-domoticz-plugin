import config


def params(**kw):
    base = {"HardwareID": "7", "Username": "ABCDEF0123456789", "Password": "", "Language": "en"}
    base.update(kw)
    return base


def test_defaults():
    cfg = config.parse_config(params())
    assert cfg.client_id == "ABCDEF0123456789"
    assert cfg.client_secret == ""
    assert cfg.allow_control is False
    assert cfg.sign_out is False
    assert cfg.debug_level == 0
    assert cfg.screen_messages == "problems"
    assert (cfg.language, cfg.accept_language) == ("en", "en-GB")
    assert cfg.hardware_id == "7"
    assert cfg.warnings == ()


def test_auto_language_follows_domoticz():
    cfg = config.parse_config(params(Language="nl", DeviceLanguage="auto"))
    assert (cfg.language, cfg.accept_language) == ("nl", "nl-NL")


def test_explicit_language_wins():
    cfg = config.parse_config(params(Language="nl", DeviceLanguage="en"))
    assert cfg.language == "en"


def test_unknown_language_warns_and_uses_english():
    cfg = config.parse_config(params(DeviceLanguage="fr"))
    assert cfg.language == "en"
    assert any("language" in w for w in cfg.warnings)


def test_booleans():
    cfg = config.parse_config(params(AllowControl="true", SignOut="True"))
    assert cfg.allow_control is True
    assert cfg.sign_out is True


def test_bad_debug_level_warns():
    cfg = config.parse_config(params(DebugLevel="9"))
    assert cfg.debug_level == 0
    assert cfg.warnings


def test_client_id_whitespace_is_stripped_and_odd_characters_warn():
    cfg = config.parse_config(params(Username="  ABCDEF0123456789 "))
    assert cfg.client_id == "ABCDEF0123456789"
    assert config.parse_config(params(Username="abc def 'x'")).warnings


def test_secret_is_never_in_repr():
    cfg = config.parse_config(params(Password="s3cr3t-value"))
    assert "s3cr3t-value" not in repr(cfg)


def test_parse_domoticz_version():
    assert config.parse_domoticz_version("2026.4") == (2026, 4)
    assert config.parse_domoticz_version("2026.1 (build 18263)") == (2026, 1)
    assert config.parse_domoticz_version("2025.2-1658-gf6aac1d0a") == (2025, 2)
    assert config.parse_domoticz_version("???") is None
    assert config.parse_domoticz_version("") is None


def test_screen_messages_choice_and_an_unknown_one_warns():
    for value in ("off", "problems", "all"):
        assert config.parse_config(params(ScreenMessages=value)).screen_messages == value
    cfg = config.parse_config(params(ScreenMessages="loud"))
    assert cfg.screen_messages == "problems"
    assert any("on-screen" in w for w in cfg.warnings)


def test_update_check_choice_and_the_plugin_version():
    cfg = config.parse_config(params(Version="0.1.0-beta.4"))
    assert (cfg.update_check, cfg.plugin_version) == ("auto", "0.1.0-beta.4")
    for value in ("auto", "stable", "beta", "off"):
        assert config.parse_config(params(UpdateCheck=value)).update_check == value
    odd = config.parse_config(params(UpdateCheck="nightly"))
    assert odd.update_check == "auto"
    assert any("update check" in w for w in odd.warnings)
