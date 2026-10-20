# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""
<plugin key="homeconnect" name="Home Connect" author="Rouzax" version="0.1.0" externallink="https://github.com/Rouzax/home-connect-domoticz-plugin">
    <description>
        <h2>Home Connect</h2>
        <p>Shows your Bosch, Siemens, Neff and Gaggenau appliances from the Home Connect cloud: state, program, remaining time, settings and messages. Turn on Allow control to also operate them from Domoticz.</p>
        <h3>Sign in</h3>
        <ol>
            <li>At developer.home-connect.com, log in with the same email as your Home Connect app and register an application of type Device Flow.</li>
            <li>Enter its Client ID below and press Add or Update.</li>
            <li>Open the "Home Connect" device on the Utility tab, follow its link and approve with your Home Connect account. The link and code are also in the Domoticz log.</li>
        </ol>
        <p>The "Home Connect" device is shown while sign-in is needed and hidden once you are signed in; you then find it under Setup &gt; Devices.</p>
        <p><b>The client secret and the Home Connect tokens are stored in cleartext in the Domoticz database. Treat database backups as secrets.</b></p>
    </description>
    <params>
        <param field="Username" label="Client ID" width="320px" required="true">
            <description>The Client ID of your application on the Home Connect developer portal (developer.home-connect.com).</description>
        </param>
        <param field="Password" label="Client Secret" width="320px" password="true">
            <description>Leave empty: a Device Flow application needs no secret. Fill it in only if your application shows one.</description>
        </param>
        <param field="AllowControl" type="boolean" label="Allow control" default="false">
            <description>Lets Domoticz, including scenes, timers and scripts, choose programs, change settings and start or stop appliances. A script or a misclick can then start a program; an appliance only accepts a start while remote start is switched on at the appliance itself.</description>
        </param>
        <param field="DeviceLanguage" label="Device language" width="160px">
            <description>Language of device names and values. Names you changed yourself are kept; descriptions keep the language they were created in.</description>
            <options>
                <option label="Same as Domoticz" value="auto" default="true"/>
                <option label="English" value="en"/>
                <option label="Nederlands" value="nl"/>
            </options>
        </param>
        <param field="SignOut" type="boolean" label="Sign out" default="false">
            <description>Turn on and press Update to sign out: the plugin withdraws its access at Home Connect and stays signed out. To sign in again, turn it off and press Update.</description>
        </param>
        <param field="DebugLevel" label="Debug level" width="160px">
            <description>None for normal use. Basic logs every request; Verbose also logs every message from Home Connect and fills the log quickly.</description>
            <options>
                <option label="None" value="0" default="true"/>
                <option label="Basic" value="1"/>
                <option label="Verbose" value="2"/>
            </options>
        </param>
    </params>
</plugin>
"""

# Postponed annotations keep this file importable on an interpreter below the floor, so onStart
# can say why it stops instead of failing at import.
from __future__ import annotations

import contextlib
import queue
import sys
import time
from dataclasses import replace
from typing import TYPE_CHECKING

import DomoticzEx as Domoticz

if TYPE_CHECKING:
    import auth
    import budget
    import catalog
    import config
    import control
    import coordinator
    import domoticz_api
    import errors
    import hc_client
    import model
    import persistence
    import planner
    import scheduler
    import sse
    import text
    import workers

MIN_PYTHON = (3, 11)
HEARTBEAT_S = 5
# While commands are pending the heartbeat runs every second: an appliance's writes go one at a
# time and answers are read only on a heartbeat, so a scene of several writes would otherwise
# approach the 30 s command TTL.
FAST_HEARTBEAT_S = 1
SHUTDOWN_DEADLINE_S = 6.0
VERBOSE_FIELD_CAP = 2000
DRAIN_LIMIT = 500
DAILY_CHECK_EVERY = 60
# Retries of the discovery listing, the device-code request (both from RETRY_BACKOFF_FIRST_S) and
# a token refresh (from RETRY_LATER_S) back off to RETRY_BACKOFF_MAX_S, doubling, and start again
# after a success: a fixed short delay during a long outage would spend the background or auth
# ceiling and leave nothing for when the API returns.
RETRY_LATER_S = 300.0
RETRY_BACKOFF_FIRST_S = 60.0
RETRY_BACKOFF_MAX_S = 3600.0
REFUSED_RETRY_S = 60.0
# Unit 1 says "(last known)" once no stream has delivered an event for this long. A dropped
# stream is live again after its first reconnect backoff (at most 1.2 * workers.BACKOFF_START_S)
# plus its first event (replays arrive at once, a keep-alive within workers.WATCHDOG_S), seen at
# the next heartbeat; the margin keeps a routine reconnect from flashing "(last known)" and
# writing two history rows for it (spec 6.2).
STREAM_STALE_AFTER_S = 400.0
WORKER_RESTARTS_PER_HOUR = 3
RESTART_WINDOW_S = 3600.0
SLOW_RESTART_INTERVAL_S = 3600.0
AWAITING_TOKEN_MAX = 64
REAUTHORIZE_LEVEL = 10
REFRESH_LEVEL = 20
_MESSAGE_MAX = 150
# Home Connect haIds are 12 digits or longer brand-model-serial strings. A shorter Device ID left
# by something else must not enter the redactor, which would blank that text in every log line.
_MIN_SEEDED_ID_LEN = 12

# Indirection so tests can move the plugin's own timers forward.
_mono = time.monotonic
_wall = time.time

_state: _PluginState | None = None


def _python_ok(version=sys.version_info) -> bool:
    return tuple(version[:2]) >= MIN_PYTHON


def _load_modules() -> None:
    """Project modules use 3.11 syntax and APIs, so they are imported only after the version
    check; a top-level import would fail before onStart could explain why."""
    global auth, budget, catalog, config, control, coordinator, domoticz_api, errors, hc_client
    global model, persistence, planner, scheduler, sse, text, workers
    import auth
    import budget
    import catalog
    import config
    import control
    import coordinator
    import domoticz_api
    import errors
    import hc_client
    import model
    import persistence
    import planner
    import scheduler
    import sse
    import text
    import workers


def _new_client() -> hc_client.HCClient:
    return hc_client.HCClient()


def _new_request_worker(client, budget_, results, generation, lang) -> workers.RequestWorker:
    return workers.RequestWorker(
        client, budget_, results, generation=generation, accept_language=lang
    )


def _new_stream_worker(client, budget_, results, generation, lang) -> workers.StreamWorker:
    return workers.StreamWorker(
        client, budget_, results, generation=generation, accept_language=lang
    )


def _devices() -> dict:
    # Domoticz injects Devices and Parameters into this module's globals.
    # An empty table is still the live one: a fresh hardware entry injects {}, and falling back on
    # falsiness would make every unit creation look like a failure.
    devices = globals().get("Devices")
    return devices if devices is not None else {}


def _parameters() -> dict:
    return globals().get("Parameters") or {}


class _PluginState:
    req: workers.RequestWorker
    stream: workers.StreamWorker

    def __init__(
        self, cfg: config.Config, saved: persistence.State, saver: domoticz_api.StateSaver
    ) -> None:
        self.disabled = False
        self.cfg = cfg
        self.state = saved
        self.saver = saver
        self.model = model.Model(wall=time.time)
        self.sched = scheduler.Scheduler(time.monotonic, time.time)
        self.budget = budget.Budget(saved.ledger, saved.block_until, saved.block_length)
        # Unbounded on purpose: workers post with put_nowait and must never block on this thread.
        self.results: queue.Queue = queue.Queue()
        self.generation = 0
        self.id_hash = auth.client_id_hash(cfg.client_id)
        self.auth_state = auth.AuthState.UNAUTHORIZED
        self.auth_generation = 1
        self.tokens: persistence.Tokens | None = None
        self.flow: auth.DeviceCode | None = None
        self.code_in_flight = False
        self.flow_retry_at: float | None = None
        self.flow_backoff = RETRY_BACKOFF_FIRST_S
        self.next_poll = 0.0
        self.poll_in_flight = False
        # Refresh is single-flight here: the worker queue never coalesces token jobs.
        self.refresh_in_flight = False
        self.refresh_wanted = False
        self.refresh_next = 0.0
        self.refresh_backoff = RETRY_LATER_S
        self.after_refresh: list[scheduler.Job] = []
        self.refresh_retried: set[tuple] = set()
        # Set by a refresh, cleared by any sign of life from the stream: a stream 401 while it
        # is still set means the refreshed token was refused too (spec 7.1).
        self.stream_token_fresh = False
        self.revoke_tokens: list[str] = []
        self.revoke_in_flight = False
        # Discovery and stream.
        self.kicked = False
        self.full_rediscovery = False
        self.list_retry_at: float | None = None
        self.list_backoff = RETRY_BACKOFF_FIRST_S
        self.stream_started = False
        self.stream_dropped = False
        self.not_live_since: float | None = None
        self.depaired: set[str] = set()
        # Presentation and logging.
        self.user_message = ""
        self.labels: dict[str, str] = {}
        self.latched: set[str] = set()
        # Appliances reported DISCONNECTED in the batch being drained; logged at its end.
        self.went_offline: set[str] = set()
        self.warned: set[str] = set()
        self.blocked_selectors: list[tuple[str, int]] = []
        self.api_blocked = False
        self.secrets: set[str] = set()
        self.ha_ids: set[str] = set()
        # Supervision.
        self.restarts: list[float] = []
        self.last_restart: float | None = None
        self.slow_restarts = False
        self.workers_down = False
        self.beat = 0
        # Control (spec 7.6).
        self.coord = coordinator.Coordinator(cfg.language, _label)
        self.op_states: dict[str, str] = {}
        self.heartbeat_s = HEARTBEAT_S

    def remember(self, *values: str) -> None:
        self.secrets.update(v for v in values if v)


def _s() -> _PluginState:
    if _state is None:
        raise RuntimeError("plugin not started")
    return _state


# -- logging ------------------------------------------------------------------------------------


def _redact(line: str) -> str:
    s = _state
    if s is None:
        return line
    # Every secret seen during this run, not only the current ones: an old token or device code
    # can still sit in a queued log record after it stopped being current.
    for secret in sorted(s.secrets, key=len, reverse=True):
        line = line.replace(secret, "[redacted]")
    for ha_id in sorted(s.ha_ids | set(s.model.appliances), key=len, reverse=True):
        line = line.replace(ha_id, "[appliance]")
    return line


def _field(value: object, max_len: int) -> str:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    # Redacted before log_value cuts it to length: a secret straddling the cut would otherwise
    # leave its prefix behind, which the whole-line pass can no longer recognise.
    return text.log_value(_redact(str(value)), max_len)


def _write_log(level: str, line: str) -> None:
    domoticz_api.log(level, _redact(line))


def _emit(level: str, event: str, **fields: object) -> None:
    """One structured line per event (spec 7.11). Every line from every source, worker records
    and exception text included, passes the redactor."""
    s = _state
    debug_level = s.cfg.debug_level if s is not None else 0
    # Verbose exists to show payloads, so its values get a longer cap than the operator lines.
    max_len = VERBOSE_FIELD_CAP if level == "verbose" else 200
    if level == "verbose":
        if debug_level < 2:
            return
        level = "debug"
    elif level == "debug" and debug_level < 1:
        return
    _write_log(level, " ".join([event, *(f"{k}={_field(v, max_len)}" for k, v in fields.items())]))


def _emit_once(key: str, level: str, event: str, **fields: object) -> None:
    s = _s()
    if key not in s.latched:
        s.latched.add(key)
        _emit(level, event, **fields)


def _label(ha_id: str) -> str:
    s = _s()
    app = s.model.appliances.get(ha_id)
    return s.labels.get(ha_id) or (app.name if app is not None and app.name else "[appliance]")


def _note(name: str, **values: str) -> str:
    return catalog.note(name, _s().cfg.language, **values)


# -- auth ---------------------------------------------------------------------------------------


def _set_auth(new_state: auth.AuthState) -> None:
    s = _s()
    s.auth_state = new_state
    s.state.auth_state = new_state.value


def _submit(job: scheduler.Job) -> bool:
    """Queue a job under the current auth generation. Token and auth jobs are never dropped
    silently (spec 4.3): the caller resets its in-flight flag and retries."""
    s = _s()
    if s.req.submit(replace(job, auth_generation=s.auth_generation)):
        return True
    if job.oauth:
        _emit("status", "auth_request_not_queued", kind=job.kind, detail="retrying later")
    else:
        _emit("debug", "job_not_queued", kind=job.kind)
        _notes(s.coord.drop([job], _mono(), "queue_full"))
    return False


def _park_all() -> None:
    s = _s()
    s.req.park()
    s.stream.park()


def _backoff(delay: float) -> tuple[float, float]:
    """(when to retry, the delay after that) for a retry that backs off."""
    return _mono() + delay, min(RETRY_BACKOFF_MAX_S, delay * 2)


def _retry_device_code_later() -> None:
    s = _s()
    s.flow_retry_at, s.flow_backoff = _backoff(s.flow_backoff)


def _retry_list_later() -> None:
    s = _s()
    s.list_retry_at, s.list_backoff = _backoff(s.list_backoff)


def _request_device_code() -> None:
    s = _s()
    s.flow_retry_at = None
    if s.code_in_flight:
        return
    s.code_in_flight = _submit(scheduler.device_code_job(s.cfg.client_id))
    if not s.code_in_flight:
        _retry_device_code_later()


def _start_flow() -> None:
    s = _s()
    s.flow = None  # a device code is never reused (spec 8.1)
    s.poll_in_flight = False
    _request_device_code()


def _keeps_state_during_flow(state: auth.AuthState) -> bool:
    """States a Re-authorize leaves in place until the new grant arrives (spec 8.5): the
    current tokens are kept, and a flow that fails must not turn them into EXPIRED_CODE."""
    return state in (
        auth.AuthState.AUTHORIZED,
        auth.AuthState.NEEDS_REAUTH,
        auth.AuthState.NEEDS_USER_ACTION,
    )


def _end_flow(note: str = "") -> None:
    """A device flow ended without a grant: a state that holds tokens stays, anything else
    becomes EXPIRED_CODE."""
    s = _s()
    s.flow, s.poll_in_flight, s.flow_retry_at = None, False, None
    if s.auth_state in (auth.AuthState.NEEDS_REAUTH, auth.AuthState.NEEDS_USER_ACTION):
        _park_all()  # Re-authorize unparked the request worker; the old tokens are still dead
    elif s.auth_state != auth.AuthState.AUTHORIZED:
        _set_auth(auth.AuthState.EXPIRED_CODE)
    if note:
        s.user_message = note


def _poll() -> None:
    s = _s()
    flow = s.flow
    if flow is None:
        return
    job = scheduler.token_poll_job(s.cfg.client_id, flow.device_code, s.cfg.client_secret)
    s.poll_in_flight = _submit(job)
    if not s.poll_in_flight:
        s.next_poll = _mono() + flow.interval


def _request_refresh() -> None:
    s = _s()
    s.refresh_wanted = True
    if s.refresh_in_flight or s.tokens is None or _mono() < s.refresh_next:
        return
    s.refresh_in_flight = _submit(
        scheduler.refresh_job(s.tokens.refresh_token, s.cfg.client_secret)
    )
    if not s.refresh_in_flight:
        s.refresh_next = _mono() + REFUSED_RETRY_S


def _tokens_changed(tokens: persistence.Tokens) -> None:
    s = _s()
    s.remember(tokens.access_token, tokens.refresh_token)
    s.tokens = tokens
    s.state.tokens = tokens
    s.saver.after_token_change()  # at once, so a crash right after approval keeps the grant
    s.req.set_token(tokens.access_token, s.auth_generation)
    if s.stream_started:
        s.stream.set_token(tokens.access_token)


def _discover() -> None:
    s = _s()
    s.full_rediscovery = True
    s.list_retry_at = None
    if not _submit(s.sched.list_job(s.model)):
        _retry_list_later()


def _grant(tokens: persistence.Tokens) -> None:
    s = _s()
    s.flow, s.poll_in_flight, s.user_message = None, False, ""
    # A new grant advances the generation, so nothing queued under the old one is sent (4.3).
    s.auth_generation += 1
    s.refresh_in_flight = s.refresh_wanted = s.stream_token_fresh = False
    s.refresh_next = 0.0
    s.refresh_backoff = RETRY_LATER_S
    _notes(s.coord.drop(s.after_refresh, _mono(), "stale"))
    s.after_refresh.clear()
    s.refresh_retried.clear()
    _tokens_changed(tokens)
    _set_auth(auth.AuthState.AUTHORIZED)
    s.req.unpark()
    s.stream.unpark()
    s.latched.discard("authorization_lost")
    _emit("status", "signed_in")
    _discover()


def _lose_authorization(
    new_state: auth.AuthState, reason: str, refused: scheduler.Job | None = None
) -> None:
    s = _s()
    _set_auth(new_state)
    _park_all()
    # Writes answered 401 (held for a refreshed token, or `refused`) were never executed.
    unsent = [*s.after_refresh, refused]
    s.refresh_wanted = s.stream_token_fresh = False
    s.after_refresh.clear()
    s.refresh_retried.clear()
    _emit_once("authorization_lost", "error", "authorization_lost", reason=reason)
    _notes(s.coord.abandon(_mono(), unsent=unsent))


# -- workers ------------------------------------------------------------------------------------


def _start_request_worker() -> None:
    """Every worker gets a fresh client: HCClient.abort() is permanent, so a client that served
    a stopped worker can never serve another."""
    s = _s()
    s.generation += 1
    worker = _new_request_worker(
        _new_client(), s.budget, s.results, s.generation, s.cfg.accept_language
    )
    worker.start()
    if s.tokens is not None:
        worker.set_token(s.tokens.access_token, s.auth_generation)
    if s.auth_state != auth.AuthState.AUTHORIZED:
        worker.park()
    s.req = worker


def _start_stream_worker() -> None:
    s = _s()
    s.generation += 1
    worker = _new_stream_worker(
        _new_client(), s.budget, s.results, s.generation, s.cfg.accept_language
    )
    worker.start()
    if s.auth_state != auth.AuthState.AUTHORIZED:
        worker.park()
    elif s.stream_started and s.tokens is not None:
        worker.set_token(s.tokens.access_token)
    if s.not_live_since is None:
        s.not_live_since = _mono()  # a fresh stream has delivered nothing yet
    s.stream = worker


def _after_request_worker_restart() -> None:
    """The old queue died with its worker: re-plan what it held."""
    s = _s()
    if s.code_in_flight:
        s.code_in_flight = False
        s.flow_retry_at = _mono()
    s.poll_in_flight = False
    s.revoke_in_flight = False
    if s.refresh_in_flight:
        s.refresh_in_flight = False
        s.refresh_wanted = True
    if s.auth_state == auth.AuthState.AUTHORIZED:
        _discover()


def _supervise() -> None:
    """Restart only a dead worker: up to WORKER_RESTARTS_PER_HOUR within an hour, then one
    restart per SLOW_RESTART_INTERVAL_S until a restarted worker survives a quiet hour (4.3)."""
    s = _s()
    now = _mono()
    s.restarts = [t for t in s.restarts if now - t < RESTART_WINDOW_S]
    dead = [w for w in (s.req, s.stream) if not w.is_alive()]
    if not dead:
        if not s.restarts:
            s.slow_restarts = False
        return
    if len(s.restarts) >= WORKER_RESTARTS_PER_HOUR:
        s.slow_restarts = True
    waited = now - s.last_restart if s.last_restart is not None else SLOW_RESTART_INTERVAL_S
    if s.slow_restarts and waited < SLOW_RESTART_INTERVAL_S:
        s.workers_down = True
        names = ",".join(w.name for w in dead)
        _emit_once("workers_down", "error", "workers_down", workers=names, retry="hourly")
        return
    s.restarts.append(now)
    s.last_restart = now
    s.workers_down = False
    s.latched.discard("workers_down")
    for worker in dead:
        worker.stop()
        _emit("status", "worker_restarted", worker=worker.name)
        if worker is s.req:
            _start_request_worker()
            _after_request_worker_restart()
        else:
            _start_stream_worker()
            s.stream_dropped = True


# -- results ------------------------------------------------------------------------------------


def _handle_device_code(r: workers.Result) -> None:
    s = _s()
    s.code_in_flight = False
    code = auth.parse_device_code(r.body, _wall()) if r.status == 200 else None
    if code is None:
        if _transient(r.status):
            _retry_device_code_later()
            _emit("debug", "authorization_start_retry", status=r.status)
            return
        err = errors.parse_oauth_error(r.status, r.body) if r.status >= 400 else None
        reason = err.code if err else "unexpected_answer"
        _emit("error", "authorization_start_failed", status=r.status, reason=reason)
        _end_flow(_note("start_failed", reason=text.clean_label(reason, _MESSAGE_MAX)))
        return
    s.remember(code.device_code)
    s.flow = code
    s.flow_backoff = RETRY_BACKOFF_FIRST_S
    s.next_poll = _mono() + code.interval
    if s.auth_state == auth.AuthState.AUTHORIZED:
        s.user_message = ""  # a note left by an earlier Re-authorize no longer applies
    elif not _keeps_state_during_flow(s.auth_state):
        s.user_message = ""
        _set_auth(auth.AuthState.AWAITING_USER)
    until = planner.clock(code.expires_at)
    _emit("status", "sign_in_needed", link=code.link, code=code.user_code, valid_until=until)


def _handle_poll(r: workers.Result) -> None:
    s = _s()
    flow = s.flow
    sent = (r.job.form or {}).get("device_code")
    if flow is None or sent != flow.device_code:
        return  # an answer for a code that has since been replaced
    s.poll_in_flight = False
    if r.status == 200:
        tokens = auth.parse_tokens(r.body, _wall(), s.id_hash)
        if tokens is not None:
            _grant(tokens)
            return
    err = errors.parse_oauth_error(r.status, r.body)
    code = err.code
    if r.status < 400 or _transient(r.status) or code == "unknown":
        # As for refresh: only a 4xx with an OAuth error code is Home Connect's verdict. A
        # network failure, an unusable 200, or a proxy, captive portal or WAF page decides
        # nothing, so the flow keeps polling until its code expires.
        outcome = "pending"
    else:
        outcome = auth.poll_outcome(err)
    if outcome in ("pending", "slow_down"):
        if outcome == "slow_down":
            s.flow = flow = replace(flow, interval=flow.interval + 5)
        s.next_poll = _mono() + flow.interval
        return
    _emit("status", "sign_in_ended", outcome=outcome, reason=code)
    if outcome == "denied":
        _end_flow(_note("denied"))
    elif outcome == "error":
        _end_flow(_note("flow_failed", reason=text.clean_label(code, _MESSAGE_MAX)))
    else:
        _end_flow()


def _handle_refresh(r: workers.Result) -> None:
    s = _s()
    s.refresh_in_flight = False
    if r.job.auth_generation != s.auth_generation:
        return  # the grant it refreshed has since been replaced
    tokens = auth.parse_tokens(r.body, _wall(), s.id_hash) if r.status == 200 else None
    if tokens is not None:
        s.refresh_wanted = False
        s.refresh_next = 0.0
        s.refresh_backoff = RETRY_LATER_S
        s.stream_token_fresh = s.stream_started
        if s.flow is None:
            s.user_message = ""  # a working grant outlives an earlier failed Re-authorize note
        _tokens_changed(tokens)
        waiting, s.after_refresh = s.after_refresh, []
        for job in waiting:
            _submit(job)
        return
    err = errors.parse_oauth_error(r.status, r.body)
    # Only a 4xx carrying an OAuth error code is Home Connect's refusal. Anything else (no
    # answer, a server error, a 3xx or 204, or an HTML page from a proxy, captive portal or WAF)
    # did not come from the token endpoint's logic and is retried later.
    if r.status < 400 or _transient(r.status) or err.code == "unknown":
        s.refresh_next, s.refresh_backoff = _backoff(s.refresh_backoff)
        _emit("debug", "refresh_retry", status=r.status)
        return
    # A definite refusal never fixes itself, so it is never retried on a timer: an unmapped code
    # would otherwise spend the auth ceiling with nothing on the account device (spec 7.1).
    new_state = auth.refresh_failure(err) or auth.AuthState.NEEDS_USER_ACTION
    if err.code in auth.CLIENT_REJECTED:
        s.user_message = _note("client_rejected")
    elif err.code == "access_denied":
        # Shown to the user (escaped by the planner), never logged (spec 7.11).
        s.user_message = text.clean_label(err.description, _MESSAGE_MAX) or err.code
    elif new_state == auth.AuthState.NEEDS_USER_ACTION:
        s.user_message = _note("refresh_refused", reason=text.clean_label(err.code, _MESSAGE_MAX))
    _lose_authorization(new_state, err.code)


def _transient(status: int) -> bool:
    """No answer, a timeout, a 429 or a server error: worth asking again later."""
    return status in (0, 408, 429) or status >= 500


def _revoke_done(failed: bool, **outcome: object) -> None:
    """One best-effort attempt per token (spec 8.6): answered or refused, it is not repeated.
    The outcome is logged once at Status with the status and error key only, never the token."""
    s = _s()
    s.revoke_in_flight = False
    if s.revoke_tokens:
        s.revoke_tokens.pop(0)
    if failed:
        detail = "remove the plugin's access in your Home Connect account"
        _emit("status", "revoke_failed", **outcome, detail=detail)
    else:
        _emit("status", "revoked", **outcome)


def _handle_revoke(r: workers.Result) -> None:
    failed = r.status == 0 or r.status >= 400
    _revoke_done(failed, status=r.status, key=r.error_key or r.category or "none")


def _signature(job: scheduler.Job) -> tuple:
    # Survives the worker's own requeues and retries, which copy the job with replace().
    return (job.kind, job.ha_id, job.key, job.issued_seq)


def _refresh_and_retry(job: scheduler.Job) -> None:
    """A 401: one forced refresh, then the job once more; a second 401 for the same job means
    the new token is refused too (spec 7.1)."""
    s = _s()
    if s.auth_state != auth.AuthState.AUTHORIZED:
        return
    if _signature(job) in s.refresh_retried:
        _lose_authorization(auth.AuthState.NEEDS_REAUTH, "invalid_token", job)
        return
    s.refresh_retried.add(_signature(job))
    # A 401 means a write was not executed, so sending it again is no retry (spec 7.6); if the
    # refresh fails, abandon reports it as not sent (cancelled), not unconfirmed.
    if len(s.after_refresh) < AWAITING_TOKEN_MAX:
        s.after_refresh.append(replace(job, retried=True))
    else:
        _notes(s.coord.drop([job], _mono(), "queue_full"))
    _request_refresh()


def _answered(r: workers.Result) -> bool:
    """False when a 401 sent the job round a token refresh: that answer says nothing yet."""
    if r.status == 401:
        _refresh_and_retry(r.job)
        return False
    _s().refresh_retried.discard(_signature(r.job))
    return True


def _handle_api(r: workers.Result) -> None:
    s = _s()
    job, m = r.job, s.model
    if not _answered(r):
        return
    if r.category == errors.FORBIDDEN:
        _emit_once(
            f"forbidden:{job.kind}:{job.ha_id}",
            "status",
            "feature_refused",
            appliance=_label(job.ha_id) if job.ha_id else "",
            kind=job.kind,
            reason=r.error_key or "forbidden",
        )
    if r.category == errors.OFFLINE:
        m.set_offline(job.ha_id)
    data = errors.parse_json(r.body) if r.status == 200 else None
    # Every answer reaches the model before the scheduler sees it: negative caching compares
    # the sequence numbers this application moves (spec 7.4).
    if job.kind == "list":
        if r.status == 200:
            s.list_backoff = RETRY_BACKOFF_FIRST_S
            _apply_list(data, job)
        else:
            _retry_list_later()
    else:
        m.apply_answer(
            job.kind,
            job.ha_id,
            r.status,
            data,
            key=job.key,
            issued=job.issued_seq,
            state_stamp=job.state_stamp,
        )
    for next_job in s.sched.after_response(m, job, r.status):
        _submit(next_job)


def _apply_list(data: object, job: scheduler.Job) -> None:
    s = _s()
    m = s.model
    new = m.apply_list(data, job.issued_seq)
    for app in m.appliances.values():
        app.cache = s.state.caches.setdefault(app.ha_id, app.cache)
        # Before the first plan, so after a restart a key the catalog does not name, a program
        # and a selector level keep the name Home Connect gave them instead of a humanized key;
        # a name observed in this run wins over the stored one.
        app.names = {**app.cache.names, **app.names}
        app.value_names = {**app.cache.value_names, **app.value_names}
        if app.connected:
            s.depaired.discard(app.ha_id)
    targets = list(m.appliances) if s.full_rediscovery else new
    s.full_rediscovery = False
    for next_job in s.sched.discovery(m, targets):
        _submit(next_job)
    if not s.stream_started and s.tokens is not None:
        # The stream starts once the appliance list is known (spec 7.3 step 7).
        s.stream.set_token(s.tokens.access_token)
        s.stream_started = True
        s.not_live_since = _mono()


def _handle_command(r: workers.Result) -> None:
    s = _s()
    if not _answered(r):
        return
    if r.category == errors.OFFLINE:
        s.model.set_offline(r.job.ha_id)
    description = errors.parse_api_error(r.status, r.body).description if r.status >= 400 else ""
    app = s.model.appliances.get(r.job.ha_id)
    job, status, key = r.job, r.status, r.error_key
    _notes(s.coord.on_answer(job.command_id, status, r.category, key, description, _mono(), app))
    _dispatch_writes()


def _handle_verify(r: workers.Result) -> None:
    """A reconciliation GET: applied to the model like any answer (an old one can never roll
    back a newer event, spec 4.3), then it settles its write. No follow-up fetches."""
    s = _s()
    if not _answered(r):
        return
    job = r.job
    data = errors.parse_json(r.body) if r.status == 200 else None
    s.model.apply_answer(job.key, job.ha_id, r.status, data, issued=job.issued_seq)
    _notes(s.coord.on_verified(job.command_id, s.model.appliances.get(job.ha_id), _mono()))


def _handle_refused(message: workers.Refused) -> None:
    s = _s()
    job, reason = message.job, message.reason
    mono = _mono()
    if job.kind in ("device_code", "token_poll"):
        if job.kind == "device_code":
            s.code_in_flight = False
        else:
            s.poll_in_flight = False
        if reason in ("ceiling", "total"):
            _emit("status", "sign_in_stopped", reason="daily sign-in budget used up")
            _end_flow(_note("auth_exhausted"))
        elif job.kind == "device_code":
            _retry_device_code_later()
        elif s.flow is not None:
            s.next_poll = mono + s.flow.interval
    elif job.kind == "refresh":
        s.refresh_in_flight = False
        s.refresh_wanted = True
        s.refresh_next = mono + REFUSED_RETRY_S
    elif job.kind == "revoke":
        _revoke_done(True, refused=reason)
    elif job.kind in ("command", "verify") and not (
        reason == "no_token" and s.auth_state == auth.AuthState.AUTHORIZED
    ):
        app = s.model.appliances.get(job.ha_id)
        _notes(s.coord.on_refused(job.command_id, reason, app, mono, message.wait_s))
    elif reason == "no_token" and s.auth_state == auth.AuthState.AUTHORIZED:
        # The worker dropped a token Home Connect refused. Jobs it turned away wait for the
        # refreshed one; once that has arrived they go straight back.
        if s.refresh_wanted or s.refresh_in_flight:
            if len(s.after_refresh) < AWAITING_TOKEN_MAX:
                s.after_refresh.append(job)
            else:
                _notes(s.coord.drop([job], mono, "queue_full"))
        else:
            _submit(job)
    elif job.kind == "list":
        _retry_list_later()


def _handle_event(message: workers.StreamEvent) -> None:
    s = _s()
    s.stream_token_fresh = False  # the stream accepted the current token
    raw = message.event
    if text.valid_ha_id(raw.id):
        s.ha_ids.add(raw.id)
    effect = s.model.apply_event(raw)
    ha = effect.ha_id
    if ha is not None:
        s.ha_ids.add(ha)
    if s.cfg.debug_level >= 2:  # parsing the items again costs nothing below Verbose
        where = {"appliance": _label(ha)} if ha is not None else {}
        for fields in sse.log_fields(raw):
            _emit("verbose", "event", type=raw.type, **where, **fields)
    if ha is not None and ha in s.model.appliances:
        if effect.kind == "depaired":
            s.depaired.add(ha)
            _emit_once(f"depaired:{ha}", "status", "appliance_removed", appliance=_label(ha))
        elif effect.kind == "disconnected":
            s.went_offline.add(ha)
        elif effect.kind in ("connected", "paired"):
            s.depaired.discard(ha)
            s.latched.discard(f"depaired:{ha}")
        if s.model.appliances[ha].connected:
            # Back by CONNECTED, or by a live item when the CONNECTED was missed.
            s.went_offline.discard(ha)  # back within the same batch: nothing to report
            if f"offline:{ha}" in s.latched:
                s.latched.discard(f"offline:{ha}")
                _emit("status", "appliance_online", appliance=_label(ha))
    for job in s.sched.on_effect(s.model, effect):
        _submit(job)


def _handle_stream_state(message: workers.StreamState) -> None:
    s = _s()
    if message.connected:
        s.stream_token_fresh = False  # the stream accepted the current token
    if message.healthy or message.live:
        s.not_live_since = None
    elif not message.connected:
        s.stream_dropped = True
        if s.not_live_since is None:
            s.not_live_since = _mono()
    elif s.stream_dropped:
        s.stream_dropped = False
        for job in s.sched.on_stream_reconnected(s.model):
            _submit(job)


def _handle(message: object) -> None:
    s = _s()
    if getattr(message, "generation", None) not in (s.req.generation, s.stream.generation):
        return  # from a worker that has been replaced
    if isinstance(message, workers.Result):
        handlers = {
            "device_code": _handle_device_code,
            "token_poll": _handle_poll,
            "refresh": _handle_refresh,
            "revoke": _handle_revoke,
            "command": _handle_command,
            "verify": _handle_verify,
        }
        handlers.get(message.job.kind, _handle_api)(message)
    elif isinstance(message, workers.StreamEvent):
        _handle_event(message)
    elif isinstance(message, workers.StreamState):
        _handle_stream_state(message)
    elif isinstance(message, workers.NeedsRefresh):
        # A 401 for a token that was already replaced says nothing about the new one, which the
        # stream tries next.
        if s.auth_state != auth.AuthState.AUTHORIZED or not message.current:
            return
        if s.stream_token_fresh:
            # Refused straight after a refresh: one refresh per 401, as for REST (spec 7.1).
            _lose_authorization(auth.AuthState.NEEDS_REAUTH, "invalid_token")
        else:
            _request_refresh()
    elif isinstance(message, workers.LogRecord):
        _emit(message.level, message.event, **message.fields)
    elif isinstance(message, workers.Refused):
        _handle_refused(message)
    elif isinstance(message, workers.WorkerCrashed):
        _emit("error", "worker_crashed", worker=message.worker, error=message.error)


def _drain() -> None:
    s = _s()
    for _ in range(DRAIN_LIMIT):
        try:
            message = s.results.get_nowait()
        except queue.Empty:
            break
        _handle(message)
    _log_offline()


def _log_offline() -> None:
    """Log an appliance offline only when it is still offline once the batch is drained.

    The stream can deliver DISCONNECTED and CONNECTED for one appliance in the same batch; an
    offline line immediately followed by an online line says nothing an operator can act on, so
    the pair is collapsed. An offline that lasts past the batch is logged as before.
    """
    s = _s()
    for ha in sorted(s.went_offline):
        app = s.model.appliances.get(ha)
        if app is not None and not app.connected:
            _emit_once(f"offline:{ha}", "status", "appliance_offline", appliance=_label(ha))
    s.went_offline.clear()


# -- heartbeat steps ----------------------------------------------------------------------------


def _kick() -> None:
    """First heartbeat: the first network request of this run (onStart sends nothing)."""
    s = _s()
    s.kicked = True
    if s.auth_state == auth.AuthState.AUTHORIZED and s.tokens is not None:
        if auth.refresh_due(s.tokens, _wall()):
            # Refresh first: a listing sent with an expired token would only spend a 401.
            s.full_rediscovery = True
            s.after_refresh.append(s.sched.list_job(s.model))
            _request_refresh()
        else:
            _discover()
    elif s.auth_state == auth.AuthState.UNAUTHORIZED:
        _start_flow()


def _send_revokes() -> None:
    s = _s()
    if s.revoke_tokens and not s.revoke_in_flight:
        job = scheduler.revoke_job(s.revoke_tokens[0], s.cfg.client_id, s.cfg.client_secret)
        s.revoke_in_flight = _submit(job)


def _auth_timers() -> None:
    s = _s()
    mono, now = _mono(), _wall()
    retry = s.flow_retry_at
    if retry is not None and mono >= retry and s.auth_state != auth.AuthState.SIGNED_OUT:
        _request_device_code()
    flow = s.flow
    if flow is not None:
        if now >= flow.expires_at:
            detail = "choose Sign in again on the Home Connect actions device"
            _emit("status", "sign_in_code_expired", detail=detail)
            _end_flow()
        elif mono >= s.next_poll and not s.poll_in_flight:
            _poll()
    if s.auth_state != auth.AuthState.AUTHORIZED or s.tokens is None:
        return
    if s.refresh_wanted or auth.refresh_due(s.tokens, now):
        _request_refresh()
    if s.list_retry_at is not None and mono >= s.list_retry_at:
        _discover()


def _scheduled_work() -> None:
    s = _s()
    if s.auth_state == auth.AuthState.AUTHORIZED and s.beat % DAILY_CHECK_EVERY == 0:
        for job in s.sched.daily(s.model, _wall()):
            _submit(job)
    for ha in s.sched.take_capped():
        # The scheduler reports an appliance at most once an hour.
        _emit("status", "refetch_capped", appliance=_label(ha), detail="waits for daily refresh")


def _watch_block() -> None:
    s = _s()
    snapshot = s.budget.snapshot
    remaining = snapshot.block_until_mono - _mono()
    blocked = remaining > 0
    if blocked != s.api_blocked:
        s.api_blocked = blocked
        if blocked:
            _emit("status", "api_blocked", seconds=int(remaining))
        else:
            _emit("status", "api_block_ended")


def _budget_notes() -> list[str]:
    s = _s()
    snapshot = s.budget.snapshot
    if snapshot.total_used >= budget.TOTAL:
        return [_note("total_exhausted")]
    return [_note("background_exhausted")] if budget.BACKGROUND in snapshot.exhausted else []


def _account_view() -> planner.AccountView:
    s = _s()
    flow = s.flow
    notes = [s.user_message] if s.user_message else []
    if s.workers_down:
        notes.append(_note("workers_down"))
    notes += _budget_notes()
    # A pending code goes to the planner in every state, so the link is rendered as an anchor
    # in one place; NEEDS_USER_ACTION puts its message inside the template.
    return planner.AccountView(
        state=s.auth_state.value,
        user_code=flow.user_code if flow else "",
        link=flow.link if flow else "",
        valid_until=planner.clock(flow.expires_at) if flow else "",
        message=". ".join(notes),
        calls=s.budget.snapshot.total_used,
    )


def _stream_stale() -> bool:
    """Unit 1 says "(last known)" once no live stream has been seen for STREAM_STALE_AFTER_S
    (spec 6.2, 7.7). A connection that drops before its first event never counts as live, so a
    stream that keeps failing that way goes stale."""
    since = _s().not_live_since
    return since is not None and _mono() - since > STREAM_STALE_AFTER_S


def _write_devices() -> None:
    s = _s()
    warnings: list[str] = []
    s.labels = planner.display_names(s.model.appliances.values(), warnings)
    stale = _stream_stale()
    now = _wall()
    updates: list[planner.DeviceUpdate] = []
    for app in s.model.appliances.values():
        if app.ha_id in s.depaired:
            continue  # stops updating; its devices stay (spec 7.4)
        label = s.labels[app.ha_id]
        updates += planner.plan_appliance(
            app,
            s.state,
            s.cfg.language,
            now,
            stale,
            label,
            warnings,
            allow_control=s.cfg.allow_control,
            blocked=s.blocked_selectors,
        )
    needs_user = (
        s.auth_state != auth.AuthState.AUTHORIZED
        or s.flow is not None
        or s.workers_down
        or bool(_budget_notes())
    )
    updates += planner.plan_account(_account_view(), s.cfg.language, needs_user)
    updates = planner.apply_overlay(updates, s.coord.overlay())
    domoticz_api.apply_updates(_devices(), updates, s.state)
    for warning in (w for w in warnings if w not in s.warned):
        s.warned.add(warning)
        _write_log("status", warning)
    # Once per transition, so a user's own hide or show survives until the next one (spec 6.1).
    if needs_user != s.state.account_shown:
        units = [planner.ACCOUNT_TEXT, planner.ACCOUNT_ACTIONS]
        domoticz_api.set_used(_devices(), planner.ACCOUNT_DEVICE, units, needs_user)
        s.state.account_shown = needs_user


def _export_budget() -> None:
    s = _s()
    s.state.ledger, s.state.block_until, s.state.block_length = s.budget.export()


def _export_names() -> None:
    s = _s()
    model.cache_names(s.model.appliances.values(), s.ha_ids | set(s.model.appliances))


# -- control ------------------------------------------------------------------------------------


def _notes(notes: list[coordinator.Note]) -> None:
    s = _s()
    for note in notes:
        _emit(note.level, note.event, appliance=_label(note.ha_id), **note.fields)
        app = s.model.appliances.get(note.ha_id)
        # A delayed-start move is held only once Home Connect confirmed it (spec 6.3), and only
        # while the program still waits: a time held after it started would apply to the next.
        if note.hold is not None and app and app.operation_state() == "DelayedStart":
            s.state.delayed_start[note.ha_id] = note.hold


def _control_context(ha: str) -> control.Context:
    s = _s()
    program, start_waiting, start_sent = s.coord.pending(ha)
    return control.Context(
        app=s.model.appliances.get(ha),
        state=s.state,
        label=_label(ha),
        lang=s.cfg.language,
        allow_control=s.cfg.allow_control,
        authorized=s.auth_state == auth.AuthState.AUTHORIZED,
        snapshot=s.budget.snapshot,
        mono=_mono(),
        wall=_wall(),
        pending_program=program,
        pending_start=start_waiting,
        sent_start=start_sent,
        pending_keys=s.coord.pending_keys(ha),
    )


def _appliance_command(ha: str, unit: int, command: str, level: object) -> None:
    """One onCommand on an appliance unit. DeviceID is a haId, so it is never logged."""
    s = _s()
    # With control off, decide() refuses everything with the control-off message first.
    if s.cfg.allow_control and (ha, unit) in s.blocked_selectors:
        decision = control.Refusal(control.message("unmapped", s.cfg.language))
    else:
        decision = control.decide(ha, unit, command, level, _control_context(ha))
    if isinstance(decision, control.Refusal):
        device, reason = domoticz_api.unit_name(_devices(), ha, unit), decision.reason
        _emit(decision.level, decision.event, appliance=_label(ha), device=device, reason=reason)
        return
    hold = None
    if isinstance(decision, control.Hold):
        _emit("status", "delayed_start", appliance=_label(ha), detail=decision.note)
        if decision.intent is None:
            if decision.minutes is None:
                s.state.delayed_start.pop(ha, None)
            else:
                s.state.delayed_start[ha] = decision.minutes
            return
        # A move of a waiting delayed start: held once confirmed, through its Note.
        intent, hold = decision.intent, decision.minutes
    else:
        intent = decision
    if intent.note:
        _emit("status", "command_adjusted", appliance=_label(ha), detail=intent.note)
    _notes(s.coord.submit(intent, _mono(), hold=hold))
    _dispatch_writes()


def _dispatch_writes() -> None:
    """Hand the coordinator's next writes to the request worker. A Start is rebuilt here, so it
    carries what was observed after the writes before it and a delay counted from now."""
    s = _s()
    mono = _mono()
    writes, notes = s.coord.dispatch(mono)
    _notes(notes)
    for write in writes:
        intent = write.intent
        if intent.action == "start":
            ha, lang = intent.ha_id, s.cfg.language
            app, held = s.model.appliances.get(ha), s.state.delayed_start.get(ha)
            built = control.build_start(app, held, _wall(), lang, _label(ha))
            if isinstance(built, control.Refusal):
                _notes(s.coord.unsent(write, mono, built.reason))
                continue
            request, note = built
            if note:
                _emit("status", "command_adjusted", appliance=_label(intent.ha_id), detail=note)
            intent = replace(intent, request=request)
        # The TTL runs from the moment the command was given, not from its dispatch (spec 7.6).
        deadline = write.created + control.COMMAND_TTL_S
        job = control.write_job(write.id, intent, s.model.tick(), deadline)
        s.coord.sent(write, job.issued_seq)
        _submit(job)  # one that cannot be queued is reported there


def _control_tick() -> None:
    s = _s()
    mono = _mono()
    notes, verify = s.coord.tick(s.model.appliances, mono)
    _notes(notes)
    for write in verify:
        job = control.verify_job(
            write.id, write.intent, s.model.tick(), mono + control.COMMAND_TTL_S
        )
        if not _submit(job):
            app = s.model.appliances.get(write.intent.ha_id)
            _notes(s.coord.on_refused(write.id, "queue_full", app, mono))
    _dispatch_writes()
    apps = s.model.appliances.values()
    for ha in control.spent_delays(s.op_states, apps, s.state.delayed_start):
        _emit("debug", "delayed_start_spent", appliance=_label(ha))


def _pace_heartbeat() -> None:
    s = _s()
    wanted = FAST_HEARTBEAT_S if s.coord.busy() else HEARTBEAT_S
    if wanted != s.heartbeat_s:
        s.heartbeat_s = wanted
        domoticz_api.heartbeat(wanted)


# -- start --------------------------------------------------------------------------------------


def _start() -> None:
    global _state
    cfg = config.parse_config(_parameters())
    domoticz_api.debugging(cfg.debug_level)
    saved, fell_back = domoticz_api.load_state()
    s = _state = _PluginState(cfg, saved, domoticz_api.StateSaver(saved))
    s.remember(cfg.client_secret)
    # Existing devices are named after appliances: their haIds must be redacted from the first
    # line on, before discovery has told the model about them.
    s.ha_ids.update(
        d
        for d in _devices()
        if d != planner.ACCOUNT_DEVICE and text.valid_ha_id(d) and len(d) >= _MIN_SEEDED_ID_LEN
    )
    for warning in cfg.warnings:
        _emit("status", "setting_adjusted", detail=warning)
    version = config.parse_domoticz_version(str(_parameters().get("DomoticzVersion", "")))
    if version is not None and version < config.RECOMMENDED_VERSION:
        _emit(
            "status",
            "domoticz_version",
            version=f"{version[0]}.{version[1]}",
            detail="Domoticz 2026.4 or newer is recommended: older versions show plugin "
            "password fields to non-admin users",
        )
    if not cfg.client_id:
        s.disabled = True
        _emit(
            "error",
            "client_id_missing",
            detail="enter the Client ID of your Device Flow application in the hardware settings",
        )
        return
    if fell_back and _devices():
        _emit("error", "state_reset", detail="stored state was unreadable; rebuilt from devices")
    # Every start, not only after a fallback: a partly lost snapshot is repaired here (7.3).
    s.blocked_selectors = domoticz_api.recover_from_devices(_devices(), saved)
    for device_id, number in s.blocked_selectors:
        _emit(
            "error",
            "selector_unmapped",
            device=domoticz_api.unit_name(_devices(), device_id, number),
            detail="commands to it are refused until a backup is restored or it is deleted",
        )
    if cfg.allow_control != saved.control_shown:
        # Once per transition, so a user's own hide or show survives until the next (spec
        # 6.4). The plugin restarts whenever a hardware setting changes, so this is the only
        # place AllowControl can change.
        for device_id in list(_devices()):
            if device_id != planner.ACCOUNT_DEVICE:
                domoticz_api.set_used(
                    _devices(), device_id, planner.CONTROL_ONLY_UNITS, cfg.allow_control
                )
        saved.control_shown = cfg.allow_control
    if auth.issued_to_another_client(saved.tokens, s.id_hash):
        _emit(
            "status",
            "previous_app_not_revoked",
            detail="the Client ID changed; remove the previous app's access in your Home Connect "
            "account",
        )
    state, keep, revoke = auth.start_state(
        saved.auth_state, saved.tokens, s.id_hash, cfg.sign_out, saved.last_sign_out
    )
    saved.last_sign_out = cfg.sign_out
    for tokens in (keep, revoke, saved.tokens):
        if tokens is not None:
            s.remember(tokens.access_token, tokens.refresh_token)
    if revoke is not None:
        # Sign out: the old grant ends here; queued jobs die with it.
        s.revoke_tokens.append(revoke.refresh_token)
        s.auth_generation += 1
    s.tokens = saved.tokens = keep
    _set_auth(state)
    _start_request_worker()
    _start_stream_worker()
    domoticz_api.heartbeat(HEARTBEAT_S)
    if fell_back:
        # Write now, so unreadable stored text does not linger until something else changes.
        try:
            domoticz_api.save_state(saved)
        except Exception as exc:  # the framework call can raise anything
            _emit("error", "state_not_saved", error=type(exc).__name__)
        s.saver = domoticz_api.StateSaver(saved)
    else:
        s.saver.after_token_change()
    _emit("status", "started", account=catalog.sign_in_state(state.value))


# -- Domoticz callbacks -------------------------------------------------------------------------


def onStart():
    global _state
    _state = None
    if not _python_ok():
        Domoticz.Error(
            f"Home Connect needs Python 3.11 or newer; this Domoticz runs {sys.version.split()[0]}"
        )
        return
    try:
        _load_modules()
        _start()
    except Exception as exc:
        Domoticz.Error(f"Home Connect could not start ({type(exc).__name__})")
        if _state is not None:
            # onStop skips a disabled plugin, so workers already running are stopped here.
            _state.disabled = True
            with contextlib.suppress(Exception):
                _stop_workers(_state)


def _stop_workers(s: _PluginState) -> list[str]:
    """Stop the workers that were started; the names of any still running at the deadline."""
    # One deadline shared by both joins: Domoticz abandons plugin threads after 10 s (7.9).
    deadline = _mono() + SHUTDOWN_DEADLINE_S
    started = [w for w in (getattr(s, "stream", None), getattr(s, "req", None)) if w is not None]
    for worker in started:
        worker.stop()
    for worker in started:
        worker.join(max(0.0, deadline - _mono()))
    return [w.name for w in started if w.is_alive()]


def _finish_stop(s: _PluginState) -> None:
    alive = _stop_workers(s)
    if alive:
        _emit("error", "worker_still_running", workers=",".join(alive))
    _export_budget()
    _export_names()
    s.saver.on_stop()


def onStop():
    s = _state
    if s is None or s.disabled:
        return
    try:
        # Before the workers stop: queued writes are never sent and handed-over ones may have
        # run (spec 4.3), so each is reported now. The devices are written again so commanded
        # values give way to the appliance's. Own guard: a failure must not keep workers running.
        _notes(s.coord.abandon(_mono(), "stopping"))
        _write_devices()
    except Exception as exc:
        Domoticz.Error(f"Home Connect: stop failed ({type(exc).__name__})")
    try:
        _finish_stop(s)
    except Exception as exc:
        Domoticz.Error(f"Home Connect: stop failed ({type(exc).__name__})")


def onHeartbeat():
    s = _state
    if s is None or s.disabled:
        return
    try:
        s.beat += 1
        _drain()
        if not s.kicked:
            _kick()
        _send_revokes()
        _auth_timers()
        _scheduled_work()
        _control_tick()
        _supervise()
        _export_budget()
        _watch_block()
        _write_devices()
        _export_names()
        s.saver.on_heartbeat()
        _pace_heartbeat()
    except Exception as exc:
        Domoticz.Error(f"Home Connect: heartbeat failed ({type(exc).__name__})")


def onCommand(DeviceID, Unit, Command, Level, Color):
    s = _state
    if s is None or s.disabled:
        return
    try:
        if DeviceID == planner.ACCOUNT_DEVICE and Unit == planner.ACCOUNT_ACTIONS:
            _account_action(int(Level or 0))
            return
        _appliance_command(str(DeviceID), int(Unit), str(Command), Level)
        _write_devices()  # a commanded value shows at once, as a request (spec 7.6)
        _pace_heartbeat()
    except Exception as exc:
        Domoticz.Error(f"Home Connect: command failed ({type(exc).__name__})")


def _account_action(level: int) -> None:
    s = _s()
    if level == REAUTHORIZE_LEVEL:
        if s.auth_state == auth.AuthState.SIGNED_OUT:
            _emit(
                "status", "reauthorize_refused", detail="turn off Sign out in the hardware settings"
            )
            return
        if s.auth_state in (auth.AuthState.NEEDS_REAUTH, auth.AuthState.NEEDS_USER_ACTION):
            s.req.unpark()  # spec 8.5
        _emit("status", "reauthorize_requested")
        # Keeps the current tokens until the new grant arrives (spec 8.5).
        _start_flow()
    elif level == REFRESH_LEVEL:
        if s.auth_state == auth.AuthState.AUTHORIZED:
            _discover()
        else:
            _emit("status", "refresh_appliances_refused", detail="not signed in")
