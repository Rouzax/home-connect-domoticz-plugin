"""The OAuth device flow, token refresh and revoke as seen from the plugin shell (spec 8); pure
state decisions live in auth.py."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import auth
import catalog
import coordinator
import errors
import persistence
import planner
import scheduler
import text
import workers

# Retries of the discovery listing, the device-code request (both from RETRY_BACKOFF_FIRST_S) and
# a token refresh (from RETRY_LATER_S) back off to RETRY_BACKOFF_MAX_S, doubling, and start again
# after a success: a fixed short delay during a long outage would spend the background or auth
# ceiling and leave nothing for when the API returns.
RETRY_LATER_S = 300.0
RETRY_BACKOFF_FIRST_S = 60.0
RETRY_BACKOFF_MAX_S = 3600.0
REFUSED_RETRY_S = 60.0
REAUTHORIZE_LEVEL = 10
REFRESH_LEVEL = 20
_MESSAGE_MAX = 150


@dataclass(frozen=True)
class Hooks:
    """What the sign-in flow needs from the plugin shell."""

    emit: Callable[..., None]  # (level, event, **fields)
    emit_once: Callable[..., None]  # (key, level, event, **fields)
    submit: Callable[[scheduler.Job], bool]
    notes: Callable[[list[coordinator.Note]], None]
    park_all: Callable[[], None]
    discover: Callable[[], None]
    mono: Callable[[], float]
    wall: Callable[[], float]


# `s` is the plugin's _PluginState; typed loosely so this module imports nothing from plugin.


def _note(s: Any, name: str, **values: str) -> str:
    return catalog.note(name, s.cfg.language, **values)


def set_auth(s: Any, new_state: auth.AuthState) -> None:
    s.auth_state = new_state
    s.state.auth_state = new_state.value


def backoff(hooks: Hooks, delay: float) -> tuple[float, float]:
    """(when to retry, the delay after that) for a retry that backs off."""
    return hooks.mono() + delay, min(RETRY_BACKOFF_MAX_S, delay * 2)


def retry_device_code_later(s: Any, hooks: Hooks) -> None:
    s.flow_retry_at, s.flow_backoff = backoff(hooks, s.flow_backoff)


def request_device_code(s: Any, hooks: Hooks) -> None:
    s.flow_retry_at = None
    if s.code_in_flight:
        return
    s.code_in_flight = hooks.submit(scheduler.device_code_job(s.cfg.client_id))
    if not s.code_in_flight:
        retry_device_code_later(s, hooks)


def start_flow(s: Any, hooks: Hooks) -> None:
    s.flow = None  # a device code is never reused (spec 8.1)
    s.poll_in_flight = False
    request_device_code(s, hooks)


def keeps_state_during_flow(state: auth.AuthState) -> bool:
    """States a Re-authorize leaves in place until the new grant arrives (spec 8.5): the
    current tokens are kept, and a flow that fails must not turn them into EXPIRED_CODE."""
    return state in (
        auth.AuthState.AUTHORIZED,
        auth.AuthState.NEEDS_REAUTH,
        auth.AuthState.NEEDS_USER_ACTION,
    )


def end_flow(s: Any, hooks: Hooks, note: str = "") -> None:
    """A device flow ended without a grant: a state that holds tokens stays, anything else
    becomes EXPIRED_CODE."""
    s.flow, s.poll_in_flight, s.flow_retry_at = None, False, None
    if s.auth_state in (auth.AuthState.NEEDS_REAUTH, auth.AuthState.NEEDS_USER_ACTION):
        hooks.park_all()  # Re-authorize unparked the request worker; the old tokens are still dead
    elif s.auth_state != auth.AuthState.AUTHORIZED:
        set_auth(s, auth.AuthState.EXPIRED_CODE)
    if note:
        s.user_message = note


def poll(s: Any, hooks: Hooks) -> None:
    flow = s.flow
    if flow is None:
        return
    job = scheduler.token_poll_job(s.cfg.client_id, flow.device_code, s.cfg.client_secret)
    s.poll_in_flight = hooks.submit(job)
    if not s.poll_in_flight:
        s.next_poll = hooks.mono() + flow.interval


def request_refresh(s: Any, hooks: Hooks) -> None:
    s.refresh_wanted = True
    if s.refresh_in_flight or s.tokens is None or hooks.mono() < s.refresh_next:
        return
    s.refresh_in_flight = hooks.submit(
        scheduler.refresh_job(s.tokens.refresh_token, s.cfg.client_secret)
    )
    if not s.refresh_in_flight:
        s.refresh_next = hooks.mono() + REFUSED_RETRY_S


def tokens_changed(s: Any, tokens: persistence.Tokens) -> None:
    s.remember(tokens.access_token, tokens.refresh_token)
    s.tokens = tokens
    s.state.tokens = tokens
    s.saver.after_token_change()  # at once, so a crash right after approval keeps the grant
    s.req.set_token(tokens.access_token, s.auth_generation)
    if s.stream_started:
        s.stream.set_token(tokens.access_token)


def grant(s: Any, hooks: Hooks, tokens: persistence.Tokens) -> None:
    s.flow, s.poll_in_flight, s.user_message = None, False, ""
    # A new grant advances the generation, so nothing queued under the old one is sent (4.3).
    s.auth_generation += 1
    s.refresh_in_flight = s.refresh_wanted = s.stream_token_fresh = False
    s.refresh_next = 0.0
    s.refresh_backoff = RETRY_LATER_S
    hooks.notes(s.coord.drop(s.after_refresh, hooks.mono(), "stale"))
    s.after_refresh.clear()
    s.refresh_retried.clear()
    tokens_changed(s, tokens)
    set_auth(s, auth.AuthState.AUTHORIZED)
    s.req.unpark()
    s.stream.unpark()
    s.latched.discard("authorization_lost")
    hooks.emit("status", "signed_in")
    hooks.discover()


def lose_authorization(
    s: Any,
    hooks: Hooks,
    new_state: auth.AuthState,
    reason: str,
    refused: scheduler.Job | None = None,
) -> None:
    set_auth(s, new_state)
    hooks.park_all()
    # Writes answered 401 (held for a refreshed token, or `refused`) were never executed.
    unsent = [*s.after_refresh, refused]
    s.refresh_wanted = s.stream_token_fresh = False
    s.after_refresh.clear()
    s.refresh_retried.clear()
    hooks.emit_once("authorization_lost", "error", "authorization_lost", reason=reason)
    hooks.notes(s.coord.abandon(hooks.mono(), unsent=unsent))


def handle_device_code(s: Any, hooks: Hooks, r: workers.Result) -> None:
    s.code_in_flight = False
    code = auth.parse_device_code(r.body, hooks.wall()) if r.status == 200 else None
    if code is None:
        if transient(r.status):
            retry_device_code_later(s, hooks)
            hooks.emit("debug", "authorization_start_retry", status=r.status)
            return
        err = errors.parse_oauth_error(r.status, r.body) if r.status >= 400 else None
        reason = err.code if err else "unexpected_answer"
        hooks.emit("error", "authorization_start_failed", status=r.status, reason=reason)
        end_flow(s, hooks, _note(s, "start_failed", reason=text.clean_label(reason, _MESSAGE_MAX)))
        return
    s.remember(code.device_code)
    s.flow = code
    s.flow_backoff = RETRY_BACKOFF_FIRST_S
    s.next_poll = hooks.mono() + code.interval
    if s.auth_state == auth.AuthState.AUTHORIZED:
        s.user_message = ""  # a note left by an earlier Re-authorize no longer applies
    elif not keeps_state_during_flow(s.auth_state):
        s.user_message = ""
        set_auth(s, auth.AuthState.AWAITING_USER)
    until = planner.clock(code.expires_at)
    hooks.emit("status", "sign_in_needed", link=code.link, code=code.user_code, valid_until=until)


def handle_poll(s: Any, hooks: Hooks, r: workers.Result) -> None:
    flow = s.flow
    sent = (r.job.form or {}).get("device_code")
    if flow is None or sent != flow.device_code:
        return  # an answer for a code that has since been replaced
    s.poll_in_flight = False
    if r.status == 200:
        tokens = auth.parse_tokens(r.body, hooks.wall(), s.id_hash)
        if tokens is not None:
            grant(s, hooks, tokens)
            return
    err = errors.parse_oauth_error(r.status, r.body)
    code = err.code
    if r.status < 400 or transient(r.status) or code == "unknown":
        # As for refresh: only a 4xx with an OAuth error code is Home Connect's verdict. A
        # network failure, an unusable 200, or a proxy, captive portal or WAF page decides
        # nothing, so the flow keeps polling until its code expires.
        outcome = "pending"
    else:
        outcome = auth.poll_outcome(err)
    if outcome in ("pending", "slow_down"):
        if outcome == "slow_down":
            s.flow = flow = replace(flow, interval=flow.interval + 5)
        s.next_poll = hooks.mono() + flow.interval
        return
    hooks.emit("status", "sign_in_ended", outcome=outcome, reason=code)
    if outcome == "denied":
        end_flow(s, hooks, _note(s, "denied"))
    elif outcome == "error":
        end_flow(s, hooks, _note(s, "flow_failed", reason=text.clean_label(code, _MESSAGE_MAX)))
    else:
        end_flow(s, hooks)


def handle_refresh(s: Any, hooks: Hooks, r: workers.Result) -> None:
    s.refresh_in_flight = False
    if r.job.auth_generation != s.auth_generation:
        return  # the grant it refreshed has since been replaced
    tokens = auth.parse_tokens(r.body, hooks.wall(), s.id_hash) if r.status == 200 else None
    if tokens is not None:
        s.refresh_wanted = False
        s.refresh_next = 0.0
        s.refresh_backoff = RETRY_LATER_S
        s.stream_token_fresh = s.stream_started
        if s.flow is None:
            s.user_message = ""  # a working grant outlives an earlier failed Re-authorize note
        tokens_changed(s, tokens)
        waiting, s.after_refresh = s.after_refresh, []
        for job in waiting:
            hooks.submit(job)
        return
    err = errors.parse_oauth_error(r.status, r.body)
    # Only a 4xx carrying an OAuth error code is Home Connect's refusal. Anything else (no
    # answer, a server error, a 3xx or 204, or an HTML page from a proxy, captive portal or WAF)
    # did not come from the token endpoint's logic and is retried later.
    if r.status < 400 or transient(r.status) or err.code == "unknown":
        s.refresh_next, s.refresh_backoff = backoff(hooks, s.refresh_backoff)
        hooks.emit("debug", "refresh_retry", status=r.status)
        return
    # A definite refusal never fixes itself, so it is never retried on a timer: an unmapped code
    # would otherwise spend the auth ceiling with nothing on the account device (spec 7.1).
    new_state = auth.refresh_failure(err) or auth.AuthState.NEEDS_USER_ACTION
    if err.code in auth.CLIENT_REJECTED:
        s.user_message = _note(s, "client_rejected")
    elif err.code == "access_denied":
        # Shown to the user (escaped by the planner), never logged (spec 7.11).
        s.user_message = text.clean_label(err.description, _MESSAGE_MAX) or err.code
    elif new_state == auth.AuthState.NEEDS_USER_ACTION:
        s.user_message = _note(
            s, "refresh_refused", reason=text.clean_label(err.code, _MESSAGE_MAX)
        )
    lose_authorization(s, hooks, new_state, err.code)


def transient(status: int) -> bool:
    """No answer, a timeout, a 429 or a server error: worth asking again later."""
    return status in (0, 408, 429) or status >= 500


def revoke_done(s: Any, hooks: Hooks, failed: bool, **outcome: object) -> None:
    """One best-effort attempt per token (spec 8.6): answered or refused, it is not repeated.
    The outcome is logged once at Status with the status and error key only, never the token."""
    s.revoke_in_flight = False
    if s.revoke_tokens:
        s.revoke_tokens.pop(0)
    if failed:
        detail = "remove the plugin's access in your Home Connect account"
        hooks.emit("status", "revoke_failed", **outcome, detail=detail)
    else:
        hooks.emit("status", "revoked", **outcome)


def handle_revoke(s: Any, hooks: Hooks, r: workers.Result) -> None:
    failed = r.status == 0 or r.status >= 400
    revoke_done(s, hooks, failed, status=r.status, key=r.error_key or r.category or "none")


def send_revokes(s: Any, hooks: Hooks) -> None:
    if s.revoke_tokens and not s.revoke_in_flight:
        job = scheduler.revoke_job(s.revoke_tokens[0], s.cfg.client_id, s.cfg.client_secret)
        s.revoke_in_flight = hooks.submit(job)


def auth_timers(s: Any, hooks: Hooks) -> None:
    mono, now = hooks.mono(), hooks.wall()
    retry = s.flow_retry_at
    if retry is not None and mono >= retry and s.auth_state != auth.AuthState.SIGNED_OUT:
        request_device_code(s, hooks)
    flow = s.flow
    if flow is not None:
        if now >= flow.expires_at:
            detail = "choose Sign in again on the Home Connect actions device"
            hooks.emit("status", "sign_in_code_expired", detail=detail)
            end_flow(s, hooks)
        elif mono >= s.next_poll and not s.poll_in_flight:
            poll(s, hooks)
    if s.auth_state != auth.AuthState.AUTHORIZED or s.tokens is None:
        return
    if s.refresh_wanted or auth.refresh_due(s.tokens, now):
        request_refresh(s, hooks)
    if s.list_retry_at is not None and mono >= s.list_retry_at:
        hooks.discover()


def account_action(s: Any, hooks: Hooks, level: int) -> None:
    if level == REAUTHORIZE_LEVEL:
        if s.auth_state == auth.AuthState.SIGNED_OUT:
            hooks.emit(
                "status", "reauthorize_refused", detail="turn off Sign out in the hardware settings"
            )
            return
        if s.auth_state in (auth.AuthState.NEEDS_REAUTH, auth.AuthState.NEEDS_USER_ACTION):
            s.req.unpark()  # spec 8.5
        hooks.emit("status", "reauthorize_requested")
        # Keeps the current tokens until the new grant arrives (spec 8.5).
        start_flow(s, hooks)
    elif level == REFRESH_LEVEL:
        if s.auth_state == auth.AuthState.AUTHORIZED:
            hooks.discover()
        else:
            hooks.emit("status", "refresh_appliances_refused", detail="not signed in")
