import json

import pytest

import auth
import errors
import persistence

H = auth.client_id_hash("CLIENT-ID-0001")
VERIFY = "https://api.home-connect.com/security/oauth/device_verify"


def tokens(h=H):
    return persistence.Tokens("a", "r", 2_000_000_000.0, auth.SCOPE, h)


def device_body(**overrides):
    body = {
        "device_code": "DC",
        "user_code": "ABCD-EFGH",
        "interval": 5,
        "expires_in": 1810,
        "verification_uri": VERIFY,
        "verification_uri_complete": VERIFY + "?user_code=ABCD-EFGH",
    }
    body.update(overrides)
    return json.dumps({k: v for k, v in body.items() if v is not None}).encode()


def test_hash_is_stable_and_short():
    assert auth.client_id_hash("x") == auth.client_id_hash("x")
    assert len(H) == 16
    assert auth.client_id_hash("x") != auth.client_id_hash("y")


def test_parse_device_code_validates_link_and_enforces_minimum_interval():
    code = auth.parse_device_code(device_body(), now=100.0)
    assert code.user_code == "ABCD-EFGH"
    assert code.link.endswith("user_code=ABCD-EFGH")
    assert code.interval == auth.MIN_POLL_S
    assert code.expires_at == 1910.0
    assert "DC" not in repr(code)


def test_parse_device_code_keeps_a_slower_server_interval():
    assert auth.parse_device_code(device_body(interval=30), now=0.0).interval == 30.0


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        (0, auth.MIN_POLL_S),
        (-5, auth.MIN_POLL_S),
        (1e999, auth.MIN_POLL_S),
        (float("nan"), auth.MIN_POLL_S),
        ("fast", auth.MIN_POLL_S),
        (True, auth.MIN_POLL_S),
        (None, auth.MIN_POLL_S),
        (1e12, auth.MAX_POLL_S),
        (30, 30.0),
    ],
)
def test_parse_device_code_clamps_the_interval_instead_of_rejecting(interval, expected):
    code = auth.parse_device_code(device_body(interval=interval), now=0.0)
    assert code is not None
    assert code.interval == expected


def test_parse_device_code_rejects_foreign_link():
    body = device_body(verification_uri="https://evil.example/x", verification_uri_complete=None)
    assert auth.parse_device_code(body, now=0.0) is None


def test_parse_device_code_never_shows_an_unsafe_complete_link():
    body = device_body(verification_uri_complete="https://evil.example/x?user_code=A")
    code = auth.parse_device_code(body, now=0.0)
    assert code.link == VERIFY


def test_parse_device_code_rejects_unsafe_complete_link_without_fallback():
    body = device_body(verification_uri_complete="javascript:alert(1)", verification_uri=None)
    assert auth.parse_device_code(body, now=0.0) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"device_code": None},
        {"user_code": None},
        {"user_code": 7},
        {"user_code": "\x00\x01"},
        {"expires_in": None},
        {"expires_in": "soon"},
        {"expires_in": 0},
        {"expires_in": -5},
        {"expires_in": 1e999},
    ],
)
def test_parse_device_code_rejects_unusable_fields(overrides):
    assert auth.parse_device_code(device_body(**overrides), now=0.0) is None


def test_parse_device_code_bounds_the_user_code():
    code = auth.parse_device_code(device_body(user_code="A" * 500), now=0.0)
    assert len(code.user_code) <= 20


@pytest.mark.parametrize("body", [b"garbage", b"[]", b'"x"', b"", b"\xff\xfe", b"[" * 200_000])
def test_parsers_survive_hostile_bodies(body):
    assert auth.parse_device_code(body, now=0.0) is None
    assert auth.parse_tokens(body, now=0.0, id_hash=H) is None


def test_parse_tokens():
    body = json.dumps(
        {
            "access_token": "A",
            "refresh_token": "R",
            "expires_in": 86400,
            "scope": auth.SCOPE,
            "token_type": "Bearer",
        }
    ).encode()
    t = auth.parse_tokens(body, now=10.0, id_hash=H)
    assert (t.access_token, t.refresh_token, t.expires_at, t.client_id_hash) == (
        "A",
        "R",
        86410.0,
        H,
    )
    assert auth.parse_tokens(b'{"access_token": "A"}', now=0.0, id_hash=H) is None
    assert auth.parse_tokens(b"garbage", now=0.0, id_hash=H) is None


def test_parse_tokens_defaults_scope_and_lifetime():
    body = json.dumps({"access_token": "A", "refresh_token": "R"}).encode()
    t = auth.parse_tokens(body, now=0.0, id_hash=H)
    assert (t.scope, t.expires_at) == (auth.SCOPE, 86400.0)


@pytest.mark.parametrize("expires_in", ["soon", None, 0, -1, 1e999, [1]])
def test_parse_tokens_rejects_unusable_lifetime(expires_in):
    body = json.dumps(
        {"access_token": "A", "refresh_token": "R", "expires_in": expires_in}
    ).encode()
    assert auth.parse_tokens(body, now=0.0, id_hash=H) is None


def test_refresh_due_an_hour_before_expiry():
    t = tokens()
    assert not auth.refresh_due(t, t.expires_at - 3601)
    assert auth.refresh_due(t, t.expires_at - 3599)


def test_start_state_rules():
    S = auth.AuthState
    assert auth.start_state("AUTHORIZED", tokens(), H, False, False) == (
        S.AUTHORIZED,
        tokens(),
        None,
    )
    assert auth.start_state("UNAUTHORIZED", None, H, False, False) == (S.UNAUTHORIZED, None, None)
    # sign-out transition revokes; staying signed out does nothing more
    assert auth.start_state("AUTHORIZED", tokens(), H, True, False) == (
        S.SIGNED_OUT,
        None,
        tokens(),
    )
    assert auth.start_state("SIGNED_OUT", None, H, True, True) == (S.SIGNED_OUT, None, None)
    # client id changed: a new flow starts; the old grant is dropped, not revoked, because
    # only the client it was issued to can revoke it
    other = tokens(h="different")
    assert auth.start_state("AUTHORIZED", other, H, False, False) == (S.UNAUTHORIZED, None, None)
    assert auth.start_state("AUTHORIZED", other, H, True, False) == (S.SIGNED_OUT, None, None)
    assert auth.issued_to_another_client(other, H)
    assert not auth.issued_to_another_client(tokens(), H)
    assert not auth.issued_to_another_client(None, H)
    # a state that needs the user survives a restart
    assert auth.start_state("NEEDS_REAUTH", tokens(), H, False, False)[0] == S.NEEDS_REAUTH
    assert auth.start_state("NEEDS_USER_ACTION", tokens(), H, False, False)[0] == (
        S.NEEDS_USER_ACTION
    )


@pytest.mark.parametrize("saved", ["AWAITING_USER", "EXPIRED_CODE"])
def test_restart_mid_authorization_starts_a_fresh_flow(saved):
    # The device code lived only in memory, so a restart must not wait on a dead code.
    assert auth.start_state(saved, None, H, False, False) == (
        auth.AuthState.UNAUTHORIZED,
        None,
        None,
    )


def test_reauthorize_in_flight_restart_keeps_the_current_tokens():
    # Re-authorize keeps the tokens while a new flow runs; a restart then resumes with them.
    assert auth.start_state("AWAITING_USER", tokens(), H, False, False) == (
        auth.AuthState.AUTHORIZED,
        tokens(),
        None,
    )


def test_signed_out_without_the_setting_starts_a_new_flow():
    assert auth.start_state("SIGNED_OUT", None, H, False, True)[0] == auth.AuthState.UNAUTHORIZED


def test_unknown_saved_state_with_tokens_is_authorized():
    assert auth.start_state("BOGUS", tokens(), H, False, False)[0] == auth.AuthState.AUTHORIZED


def test_poll_outcomes():
    def e(code, status=400):
        return errors.OAuthError(status, code, "")

    assert auth.poll_outcome(e("authorization_pending")) == "pending"
    assert auth.poll_outcome(e("slow_down")) == "slow_down"
    assert auth.poll_outcome(e("access_denied", 403)) == "denied"
    assert auth.poll_outcome(e("expired_token")) == "expired"
    assert auth.poll_outcome(e("weird")) == "error"


def test_refresh_failures():
    S = auth.AuthState
    assert auth.refresh_failure(errors.OAuthError(400, "invalid_grant", "")) == S.NEEDS_REAUTH
    assert (
        auth.refresh_failure(errors.OAuthError(403, "access_denied", "terms"))
        == S.NEEDS_USER_ACTION
    )
    for code in ("invalid_client", "unauthorized_client"):
        failure = auth.refresh_failure(errors.OAuthError(401, code, ""))
        assert failure == S.NEEDS_USER_ACTION
        assert code in auth.CLIENT_REJECTED
    assert auth.refresh_failure(errors.OAuthError(400, "invalid_scope", "")) is None
    assert auth.refresh_failure(errors.OAuthError(429, "unknown", "")) is None
    assert auth.refresh_failure(errors.OAuthError(503, "unknown", "")) is None
