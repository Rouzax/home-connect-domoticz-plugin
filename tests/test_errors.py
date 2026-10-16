import json
from pathlib import Path

import errors
from errors import _USER_KEYS, _USER_MESSAGES

CASES = json.loads((Path(__file__).parent / "fixtures" / "api_error_keys.json").read_text())


def body(key, description="d"):
    return json.dumps({"error": {"key": key, "description": description}}).encode()


def test_every_documented_key_gets_a_known_category():
    for case in CASES:
        err = errors.parse_api_error(case["status"], body(case["key"]))
        assert err.category != errors.UNKNOWN, case


def test_selected_categories():
    assert errors.parse_api_error(404, body("SDK.Error.NoProgramActive")).category == errors.STATE
    assert (
        errors.parse_api_error(409, body("SDK.Error.WrongOperationState")).category == errors.STATE
    )
    off = "SDK.Error.HomeAppliance.Connection.Initialization.Failed"
    assert errors.parse_api_error(409, body(off)).category == errors.OFFLINE
    remote = "BSH.Common.Error.RemoteStartNotActive"
    assert errors.parse_api_error(409, body(remote)).category == errors.REFUSED_USER
    bad = "SDK.Error.InvalidOptionValue"
    assert errors.parse_api_error(409, body(bad)).category == errors.REFUSED_VALIDATION
    assert errors.parse_api_error(401, body("invalid_token")).category == errors.AUTH
    assert errors.parse_api_error(403, body("insufficient_scope")).category == errors.FORBIDDEN
    assert errors.parse_api_error(429, body("429")).category == errors.LIMITED
    assert errors.parse_api_error(503, None).category == errors.SERVER
    assert errors.parse_api_error(408, b"").category == errors.SERVER


def test_unknown_key_falls_back_to_status():
    err = errors.parse_api_error(409, body("SDK.Error.SomethingNew"))
    assert err.category == errors.REFUSED_VALIDATION
    assert errors.parse_api_error(418, b"not json").category == errors.UNKNOWN


def test_oauth_shape_is_parsed_separately():
    raw = json.dumps({"error": "authorization_pending", "error_description": "wait"}).encode()
    err = errors.parse_oauth_error(400, raw)
    assert (err.code, err.description) == ("authorization_pending", "wait")
    assert errors.parse_oauth_error(400, b"<html>").code == "unknown"


def test_oauth_body_given_to_api_parser_is_not_misread():
    raw = json.dumps({"error": "invalid_grant", "error_description": "x"}).encode()
    assert errors.parse_api_error(400, raw).key == ""


def test_brake_counting():
    assert errors.counts_for_brake(errors.STATE) is True
    assert errors.counts_for_brake(errors.NETWORK) is False


def test_user_message_is_translated_and_names_the_appliance():
    en = errors.user_message("BSH.Common.Error.RemoteStartNotActive", "Washer", "en")
    nl = errors.user_message("BSH.Common.Error.RemoteStartNotActive", "Washer", "nl")
    assert en and "Washer" in en and "remote start" in en.lower()
    assert nl and "Washer" in nl and nl != en
    assert errors.user_message("SDK.Error.NoProgramActive", "x", "en") is None


def test_deeply_nested_body_is_treated_as_unparseable():
    # json.loads raises RecursionError on this input, not ValueError.
    deep = b"[" * 100000
    nested_error = b'{"error": ' + b"[" * 100000 + b"]" * 100000 + b"}"
    for raw in (deep, nested_error):
        assert errors.parse_api_error(500, raw).category == errors.SERVER
        assert errors.parse_api_error(418, raw).category == errors.UNKNOWN
        assert errors.parse_oauth_error(400, raw).code == "unknown"


def test_null_key_is_none_not_the_text_none():
    raw = json.dumps({"error": {"key": None, "description": "d"}}).encode()
    err = errors.parse_api_error(409, raw)
    assert err.key is None
    assert err.category == errors.REFUSED_VALIDATION
    assert errors.parse_api_error(401, raw).category == errors.AUTH


def test_every_message_key_is_a_user_actionable_key():
    assert set(_USER_MESSAGES) <= _USER_KEYS


def test_user_actionable_keys_without_a_message_are_listed():
    # Deferred to the control plan; listed here so the gap stays visible.
    without_message = {
        "SDK.Error.BatteryLevelTooLow",
        "SDK.Error.ConsumerProducts.CleaningRobot.Status.Lifted",
        "SDK.Error.ConsumerProducts.CleaningRobot.Status.DustBoxNotInserted",
        "SDK.Error.ConsumerProducts.CleaningRobot.Status.AlreadyAtHome",
    }
    assert _USER_KEYS - set(_USER_MESSAGES) == without_message
    for key in without_message:
        assert errors.user_message(key, "x", "en") is None


def test_parse_json_is_safe_on_untrusted_bodies():
    assert errors.parse_json(b"garbage") is None
    assert errors.parse_json(b"") is None
    assert errors.parse_json(b"[" * 200_000) is None
    assert errors.parse_json(b'{"a": [1, 2]}') == {"a": [1, 2]}
