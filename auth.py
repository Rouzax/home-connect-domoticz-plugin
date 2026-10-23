"""OAuth device flow and token decisions (spec 8). Pure: decides, never sends."""

import hashlib
import math
from dataclasses import dataclass, field
from enum import StrEnum

import errors
import persistence
import text

# Fixed scope set regardless of AllowControl (spec D10). Settings is needed even to READ
# settings: the Monitor scope excludes them (hcsdk-production.yaml securityDefinitions).
SCOPE = "IdentifyAppliance Monitor Control Settings"
REFRESH_MARGIN_S = 3600
# Home Connect returns interval 5; every poll is a request toward the daily total, so the plugin
# never polls faster than this (spec 8.1).
MIN_POLL_S = 10.0
# Upper bound on the server's interval: a code lives about 30 minutes and must stay pollable
# even if the server returns an absurd value.
MAX_POLL_S = 60.0
_DEFAULT_TOKEN_LIFETIME_S = 86400.0
_USER_CODE_MAX_LEN = 20
# OAuth codes for a Client ID or Client Secret the token endpoint does not accept.
CLIENT_REJECTED = frozenset({"invalid_client", "unauthorized_client"})


class AuthState(StrEnum):
    # Refresh timing is a timer (refresh_due), not a state.
    UNAUTHORIZED = "UNAUTHORIZED"
    SIGNED_OUT = "SIGNED_OUT"
    AWAITING_USER = "AWAITING_USER"
    AUTHORIZED = "AUTHORIZED"
    NEEDS_REAUTH = "NEEDS_REAUTH"
    NEEDS_USER_ACTION = "NEEDS_USER_ACTION"
    EXPIRED_CODE = "EXPIRED_CODE"


@dataclass(frozen=True)
class DeviceCode:
    device_code: str = field(repr=False)
    user_code: str
    link: str
    interval: float
    expires_at: float


def client_id_hash(client_id: str) -> str:
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]


def _seconds(value: object) -> float | None:
    """A positive, finite duration; JSON allows 1e999 and booleans, neither is a lifetime."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _poll_interval(value: object) -> float:
    """The server's interval clamped to [MIN_POLL_S, MAX_POLL_S]; an unusable value never
    rejects the code, it just means the minimum."""
    seconds = _seconds(value)
    if seconds is None:
        return MIN_POLL_S
    return min(MAX_POLL_S, max(MIN_POLL_S, seconds))


def parse_device_code(body: bytes, now: float) -> DeviceCode | None:
    data = errors.parse_json(body)
    if not isinstance(data, dict):
        return None
    # Both candidates go through auth_link: the link is shown to the user, so a tampered
    # complete URI falls back to the plain one instead of passing through.
    link = text.auth_link(data.get("verification_uri_complete")) or text.auth_link(
        data.get("verification_uri")
    )
    device_code, user_code = data.get("device_code"), data.get("user_code")
    if not (link and isinstance(device_code, str) and isinstance(user_code, str)):
        return None
    shown_code = text.clean_label(user_code, _USER_CODE_MAX_LEN)
    expires_in = _seconds(data.get("expires_in"))
    if not shown_code or expires_in is None:
        return None
    return DeviceCode(
        device_code, shown_code, link, _poll_interval(data.get("interval")), now + expires_in
    )


def parse_tokens(body: bytes, now: float, id_hash: str) -> persistence.Tokens | None:
    data = errors.parse_json(body)
    if not isinstance(data, dict):
        return None
    access, refresh = data.get("access_token"), data.get("refresh_token")
    if not (isinstance(access, str) and isinstance(refresh, str)):
        return None
    expires_in = _seconds(data.get("expires_in", _DEFAULT_TOKEN_LIFETIME_S))
    if expires_in is None:
        return None
    scope = data.get("scope")
    return persistence.Tokens(
        access, refresh, now + expires_in, scope if isinstance(scope, str) else SCOPE, id_hash
    )


def refresh_due(tokens: persistence.Tokens, now: float) -> bool:
    return now >= tokens.expires_at - REFRESH_MARGIN_S


def issued_to_another_client(tokens: persistence.Tokens | None, id_hash: str) -> bool:
    return tokens is not None and tokens.client_id_hash != id_hash


def start_state(
    saved: str,
    tokens: persistence.Tokens | None,
    id_hash: str,
    sign_out: bool,
    last_sign_out: bool,
) -> tuple[AuthState, persistence.Tokens | None, persistence.Tokens | None]:
    """(state, tokens to keep, tokens to revoke) at plugin start (spec 8.4, 8.6).

    Tokens issued to another Client ID are dropped, never revoked: the revocation endpoint
    authenticates the client, so the current Client ID cannot revoke them (RFC 7009 2.1).
    """
    foreign = issued_to_another_client(tokens, id_hash)
    if sign_out:
        revoke = tokens if not last_sign_out and not foreign else None
        return AuthState.SIGNED_OUT, None, revoke
    if foreign:
        return AuthState.UNAUTHORIZED, None, None
    if saved in (AuthState.NEEDS_REAUTH, AuthState.NEEDS_USER_ACTION) and tokens is not None:
        return AuthState(saved), tokens, None
    if tokens is not None:
        return AuthState.AUTHORIZED, tokens, None
    # AWAITING_USER and EXPIRED_CODE are deliberately not resumed: the device code died with the
    # previous run, so the user gets a fresh code instead of a flow that can never complete.
    return AuthState.UNAUTHORIZED, None, None


def poll_outcome(err: errors.OAuthError) -> str:
    return {
        "authorization_pending": "pending",
        "slow_down": "slow_down",
        "access_denied": "denied",
        "expired_token": "expired",
    }.get(err.code, "error")


def refresh_failure(err: errors.OAuthError) -> AuthState | None:
    """invalid_grant also means the user revoked the client in the Home Connect app; 403
    access_denied means something like unaccepted terms of use (docs, authorization errors).
    A rejected client (CLIENT_REJECTED) needs the hardware settings fixed before Re-authorize can
    work, so it asks for user action rather than a plain re-authorization."""
    if err.code == "invalid_grant":
        return AuthState.NEEDS_REAUTH
    if err.code == "access_denied" or err.code in CLIENT_REJECTED:
        return AuthState.NEEDS_USER_ACTION
    return None
