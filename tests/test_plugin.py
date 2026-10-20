import json
import time
from dataclasses import replace

import pytest

import auth
import control
import coordinator
import domoticz_api
import errors
import hc_client
import persistence
import planner
import plugin
import sse
import workers
from tests import fakes

HA = "SIEMENS-HCS01OVN1-000000000001"
HA2 = "BOSCH-HCS02DWH1-000000000002"
CID = "CLIENTID0123456789"
SECRET = "SECRET-VALUE-123"
DEVICE_CODE = "DEVICE-CODE-VALUE"
ACCESS = "ACCESS-TOKEN-VALUE"
REFRESH = "REFRESH-TOKEN-VALUE"
ACCESS2 = "ACCESS-TOKEN-SECOND"
LINK = "https://api.home-connect.com/security/oauth/device_verify?user_code=ABCD-EFGH"
RUN = "BSH.Common.EnumType.OperationState.Run"


class Clock:
    """Plugin monotonic clock that a test can move forward."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.monotonic() + self.offset


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(plugin, "_mono", c)
    return c


@pytest.fixture
def run(domoticz, monkeypatch):
    made = {"clients": []}

    def client():
        made["clients"].append(fakes.NoClient())
        return made["clients"][-1]

    def req(client_, b, results, generation, lang):
        made["req"] = fakes.FakeRequestWorker(client_, b, results, generation, lang)
        return made["req"]

    def stream(client_, b, results, generation, lang):
        made["stream"] = fakes.FakeStreamWorker(client_, b, results, generation, lang)
        return made["stream"]

    monkeypatch.setattr(plugin, "_new_request_worker", req)
    monkeypatch.setattr(plugin, "_new_stream_worker", stream)
    monkeypatch.setattr(plugin, "_new_client", client)

    def start(**params):
        base = {
            "HardwareID": "1",
            "Username": CID,
            "Password": SECRET,
            "Language": "en",
            "DomoticzVersion": "2026.4",
            "DeviceLanguage": "en",
            "DebugLevel": "2",
        }
        base.update(params)
        plugin.Parameters = base
        plugin.Devices = domoticz.Devices
        plugin.onStart()
        return made

    return start


def device_code_body():
    body = {
        "device_code": DEVICE_CODE,
        "user_code": "ABCD-EFGH",
        "interval": 5,
        "expires_in": 1810,
        "verification_uri_complete": LINK,
    }
    return json.dumps(body).encode()


def token_body(access=ACCESS):
    body = {
        "access_token": access,
        "refresh_token": REFRESH,
        "expires_in": 86400,
        "scope": "IdentifyAppliance Monitor Control Settings",
    }
    return json.dumps(body).encode()


def oauth_error(code):
    return json.dumps({"error": code, "error_description": "SECRET DESCRIPTION"}).encode()


def appliance(ha=HA, name="Oven", kind="Oven", connected=True):
    return {
        "haId": ha,
        "name": name,
        "type": kind,
        "brand": "SIEMENS",
        "vib": "X",
        "connected": connected,
    }


def listing(*apps):
    return json.dumps({"data": {"homeappliances": list(apps or [appliance()])}}).encode()


def status_body(state=RUN):
    entries = [{"key": "BSH.Common.Status.OperationState", "value": state}]
    return json.dumps({"data": {"status": entries}}).encode()


def start_flow(made):
    """First heartbeat to an answered device code; the first poll is due."""
    plugin.onHeartbeat()
    req = made["req"]
    fakes.push(req, fakes.result(req, req.take("device_code"), 200, device_code_body()))
    plugin.onHeartbeat()
    plugin._s().next_poll = 0


def approve(made, access=ACCESS):
    """Answer the next poll with a grant."""
    plugin.onHeartbeat()
    req = made["req"]
    fakes.push(req, fakes.result(req, req.take("token_poll"), 200, token_body(access)))
    plugin.onHeartbeat()


def authorize(made):
    start_flow(made)
    approve(made)


def discover(made, *apps):
    req = made["req"]
    fakes.push(req, fakes.result(req, req.take("list"), 200, listing(*apps)))
    plugin.onHeartbeat()


def answer(made, kind, status=200, body=b"", category="", error_key=""):
    req = made["req"]
    fakes.push(req, fakes.result(req, req.take(kind), status, body, category, error_key))
    plugin.onHeartbeat()


def event(made, kind, data="", ha=HA):
    stream = made["stream"]
    fakes.push(stream, workers.StreamEvent(stream.generation, sse.Event(kind, data, ha)))


def stream_state(made, connected, healthy=False, live=False):
    stream = made["stream"]
    fakes.push(stream, workers.StreamState(stream.generation, connected, healthy, "x", live))


def log_text(domoticz):
    return "\n".join(m for _, m in domoticz._log)


def log_count(domoticz, needle):
    return sum(needle in m for _, m in domoticz._log)


def account_unit(domoticz, unit=planner.ACCOUNT_TEXT):
    return domoticz.Devices[planner.ACCOUNT_DEVICE].Units[unit]


def saved_state(domoticz):
    state, fell_back = persistence.loads(domoticz.Configuration()[persistence.STATE_KEY])
    assert not fell_back
    return state


def store(domoticz, **fields):
    domoticz.Configuration({persistence.STATE_KEY: persistence.dumps(persistence.State(**fields))})


def live_tokens(expires_in=80000.0):
    return persistence.Tokens(
        ACCESS, REFRESH, time.time() + expires_in, "s", auth.client_id_hash(CID)
    )


# -- start and settings ------------------------------------------------------------------------


def test_python_floor():
    assert plugin._python_ok((3, 11, 0))
    assert not plugin._python_ok((3, 10, 9))


def test_missing_client_id_disables_the_plugin(run, domoticz):
    run(Username="")
    plugin.onHeartbeat()
    assert any(level == "error" and "Client ID" in m for level, m in domoticz._log)
    assert plugin._s().disabled


def test_old_domoticz_warns(run, domoticz):
    run(DomoticzVersion="2026.3")
    assert "2026.4" in log_text(domoticz)


def test_start_wires_the_model_clock(run):
    run()
    assert plugin._s().model._wall is time.time


def test_failed_start_after_the_workers_started_stops_them(run, domoticz, monkeypatch):
    def broken(seconds):
        raise RuntimeError("framework refused")

    monkeypatch.setattr(domoticz_api, "heartbeat", broken)
    made = run()
    assert plugin._s().disabled
    assert made["req"].stopped and made["stream"].stopped
    assert any(level == "error" and "could not start" in m for level, m in domoticz._log)


def test_each_worker_gets_its_own_client(run):
    made = run()
    assert len(made["clients"]) == 2
    assert made["req"].client is not made["stream"].client


def test_reconcile_runs_on_every_start(run, domoticz):
    store(domoticz)  # a readable snapshot: no fallback
    unit = domoticz.Unit(
        Name="Oven Extra", DeviceID=HA, Unit=40, TypeName="Text", Options={"HCKey": "A.B.C"}
    )
    unit.Create()
    run()
    s = plugin._s()
    assert s.state.next_unit[HA] == 41
    assert s.state.unit_alloc[HA]["A.B.C"] == 40


def test_unreadable_state_is_replaced_at_once(run, domoticz):
    domoticz.Configuration({persistence.STATE_KEY: "{not json"})
    run()
    saved_state(domoticz)  # asserts the stored text parses again


def test_unmapped_selector_is_named_and_refused(run, domoticz):
    unit = domoticz.Unit(
        Name="Oven Program", DeviceID=HA, Unit=25, TypeName="Selector Switch", Switchtype=18
    )
    unit.Create()
    run(AllowControl="true")  # with control off the control-off refusal comes first
    errors_ = [m for level, m in domoticz._log if level == "error"]
    assert any("Oven Program" in m for m in errors_)
    plugin.onCommand(HA, 25, "Set Level", 10, "")
    assert "restore a backup" in log_text(domoticz)
    assert HA not in log_text(domoticz)


# -- device flow --------------------------------------------------------------------------------


def test_device_flow_end_to_end(run, domoticz):
    made = run()
    start_flow(made)
    req = made["req"]
    assert plugin._s().auth_state == "AWAITING_USER"
    assert "ABCD-EFGH" in account_unit(domoticz).sValue
    plugin.onHeartbeat()
    poll = req.take("token_poll")
    assert poll.form["client_secret"] == SECRET
    fakes.push(req, fakes.result(req, poll, 200, token_body()))
    plugin.onHeartbeat()
    assert plugin._s().auth_state == "AUTHORIZED"
    assert req.token == ACCESS
    assert saved_state(domoticz).tokens.access_token == ACCESS  # persisted at once (spec 8.2)
    req.take("list")
    assert account_unit(domoticz).stored["Used"] == 0


def test_pending_poll_reschedules(run, clock):
    made = run()
    start_flow(made)
    plugin.onHeartbeat()
    req = made["req"]
    pending = oauth_error("authorization_pending")
    poll = req.take("token_poll")
    fakes.push(req, fakes.result(req, poll, 400, pending, "oauth", "authorization_pending"))
    plugin.onHeartbeat()
    assert plugin._s().auth_state == "AWAITING_USER"
    assert plugin._s().next_poll > clock()
    assert "token_poll" not in req.kinds()


def test_denied_ends_the_flow(run, domoticz):
    made = run()
    start_flow(made)
    plugin.onHeartbeat()
    req = made["req"]
    denied = oauth_error("access_denied")
    fakes.push(req, fakes.result(req, req.take("token_poll"), 403, denied, "oauth", "x"))
    plugin.onHeartbeat()
    assert plugin._s().auth_state == "EXPIRED_CODE"
    assert "denied" in account_unit(domoticz).sValue
    assert "SECRET DESCRIPTION" not in log_text(domoticz)


def test_auth_budget_refusal_expires_the_code(run, domoticz):
    made = run()
    start_flow(made)
    plugin.onHeartbeat()
    req = made["req"]
    fakes.push(req, workers.Refused(req.generation, req.take("token_poll"), "ceiling"))
    plugin.onHeartbeat()
    s = plugin._s()
    assert s.auth_state == "EXPIRED_CODE"
    assert s.flow is None
    assert "budget" in account_unit(domoticz).sValue


def test_device_code_refused_by_budget_expires(run):
    made = run()
    plugin.onHeartbeat()
    req = made["req"]
    fakes.push(req, workers.Refused(req.generation, req.take("device_code"), "total"))
    plugin.onHeartbeat()
    assert plugin._s().auth_state == "EXPIRED_CODE"


def test_other_poll_refusals_push_the_poll_forward(run, clock):
    made = run()
    start_flow(made)
    plugin.onHeartbeat()
    req = made["req"]
    fakes.push(req, workers.Refused(req.generation, req.take("token_poll"), "queue_full"))
    plugin.onHeartbeat()
    assert plugin._s().auth_state == "AWAITING_USER"
    assert plugin._s().next_poll > clock()
    assert "token_poll" not in req.kinds()
    clock.offset += 60
    plugin.onHeartbeat()
    assert "token_poll" in req.kinds()


def test_network_failure_retries_the_device_code(run, clock):
    made = run()
    plugin.onHeartbeat()
    req = made["req"]
    fakes.push(req, fakes.result(req, req.take("device_code"), 0, category="network"))
    plugin.onHeartbeat()
    assert "device_code" not in req.kinds()
    clock.offset += plugin.RETRY_BACKOFF_FIRST_S
    plugin.onHeartbeat()
    assert "device_code" in req.kinds()


def test_code_expiry_sets_expired_code(run):
    made = run()
    start_flow(made)
    s = plugin._s()
    assert s.flow is not None
    s.flow = replace(s.flow, expires_at=0.0)
    plugin.onHeartbeat()
    assert s.auth_state == "EXPIRED_CODE"
    assert "token_poll" not in made["req"].kinds()


def test_unqueued_oauth_job_is_logged_and_retried(run, domoticz, clock):
    made = run()
    made["req"].accepting = False
    plugin.onHeartbeat()
    assert any(level == "status" and "device_code" in m for level, m in domoticz._log)
    assert not plugin._s().code_in_flight
    made["req"].accepting = True
    clock.offset += plugin.RETRY_BACKOFF_FIRST_S
    plugin.onHeartbeat()
    assert "device_code" in made["req"].kinds()


def test_secrets_and_haids_never_reach_the_log(run, domoticz):
    made = run()
    authorize(made)
    req = made["req"]
    fakes.push(req, fakes.result(req, req.take("list"), 200, listing()))
    detail = f"{ACCESS} {REFRESH} {HA} {SECRET} {DEVICE_CODE}"
    fakes.push(req, workers.LogRecord(req.generation, "debug", "request", {"detail": detail}))
    event(made, "STATUS", json.dumps({"haId": HA, "items": []}))
    plugin.onHeartbeat()
    text = log_text(domoticz)
    assert "ABCD-EFGH" in text  # the user code is the one value the user needs
    for secret in (ACCESS, REFRESH, SECRET, DEVICE_CODE, HA):
        assert secret not in text


def test_results_from_an_old_generation_are_ignored(run):
    made = run()
    plugin.onHeartbeat()
    req = made["req"]
    job = req.take("device_code")
    old = workers.Result(req.generation - 1, job, 200, device_code_body(), "", "", None, 1)
    fakes.push(req, old)
    plugin.onHeartbeat()
    assert plugin._s().auth_state == "UNAUTHORIZED"


# -- tokens -------------------------------------------------------------------------------------


def test_401_refreshes_once_and_retries(run):
    made = run()
    authorize(made)
    req = made["req"]
    fakes.push(req, fakes.result(req, req.take("list"), 401, b"", "auth", "invalid_token"))
    plugin.onHeartbeat()
    fakes.push(req, fakes.result(req, req.take("refresh"), 200, token_body(ACCESS2)))
    plugin.onHeartbeat()
    assert req.token == ACCESS2
    assert req.take("list").retried


def test_second_401_after_refresh_needs_reauth(run, domoticz):
    made = run()
    authorize(made)
    req = made["req"]
    fakes.push(req, fakes.result(req, req.take("list"), 401, b"", "auth", "invalid_token"))
    plugin.onHeartbeat()
    fakes.push(req, fakes.result(req, req.take("refresh"), 200, token_body(ACCESS2)))
    plugin.onHeartbeat()
    fakes.push(req, fakes.result(req, req.take("list"), 401, b"", "auth", "invalid_token"))
    plugin.onHeartbeat()
    plugin.onHeartbeat()
    assert plugin._s().auth_state == "NEEDS_REAUTH"
    assert req.parked and made["stream"].parked
    assert log_count(domoticz, "authorization_lost") == 1


def test_transport_retry_flag_alone_does_not_need_reauth(run):
    made = run()
    authorize(made)
    req = made["req"]
    retried = replace(req.take("list"), retried=True)  # the worker's own network retry
    fakes.push(req, fakes.result(req, retried, 401, b"", "auth", "invalid_token"))
    plugin.onHeartbeat()
    assert plugin._s().auth_state == "AUTHORIZED"
    assert "refresh" in req.kinds()


def test_refresh_is_single_flight(run):
    made = run()
    authorize(made)
    stream = made["stream"]
    fakes.push(stream, workers.NeedsRefresh(stream.generation))
    fakes.push(stream, workers.NeedsRefresh(stream.generation))
    plugin.onHeartbeat()
    plugin.onHeartbeat()
    assert made["req"].kinds().count("refresh") == 1


def test_jobs_refused_without_a_token_are_resent_after_refresh(run):
    made = run()
    authorize(made)
    req, stream = made["req"], made["stream"]
    fakes.push(stream, workers.NeedsRefresh(stream.generation))
    plugin.onHeartbeat()
    fakes.push(req, workers.Refused(req.generation, req.take("list"), "no_token"))
    plugin.onHeartbeat()
    assert "list" not in req.kinds()
    fakes.push(req, fakes.result(req, req.take("refresh"), 200, token_body(ACCESS2)))
    plugin.onHeartbeat()
    discover(made)
    assert stream.token == ACCESS2
    assert plugin._s().stream_started


def test_invalid_grant_needs_reauth_and_parks(run, domoticz):
    made = run()
    authorize(made)
    req = made["req"]
    plugin._s().tokens = live_tokens(expires_in=10)
    plugin.onHeartbeat()
    body = oauth_error("invalid_grant")
    fakes.push(req, fakes.result(req, req.take("refresh"), 400, body, "oauth", "invalid_grant"))
    plugin.onHeartbeat()
    assert plugin._s().auth_state == "NEEDS_REAUTH"
    assert made["stream"].parked
    assert account_unit(domoticz).stored["Used"] == 1


def test_access_denied_on_refresh_needs_user_action(run, domoticz):
    made = run()
    authorize(made)
    req = made["req"]
    plugin._s().tokens = live_tokens(expires_in=10)
    plugin.onHeartbeat()
    body = oauth_error("access_denied")
    fakes.push(req, fakes.result(req, req.take("refresh"), 403, body, "oauth", "access_denied"))
    plugin.onHeartbeat()
    assert plugin._s().auth_state == "NEEDS_USER_ACTION"
    assert "SECRET DESCRIPTION" in account_unit(domoticz).sValue
    assert "SECRET DESCRIPTION" not in log_text(domoticz)


def test_kick_keeps_workers_parked_when_reauth_is_needed(run, domoticz):
    store(domoticz, tokens=live_tokens(), auth_state="NEEDS_REAUTH")
    made = run()
    plugin.onHeartbeat()
    assert made["req"].parked and made["stream"].parked
    assert made["req"].jobs == []


def test_sign_out_revokes_and_clears(run, domoticz):
    store(domoticz, tokens=live_tokens(), auth_state="AUTHORIZED")
    made = run(SignOut="true")
    plugin.onHeartbeat()
    revoke = made["req"].take("revoke")
    assert revoke.form["token"] == REFRESH
    saved = saved_state(domoticz)
    assert saved.tokens is None and saved.auth_state == "SIGNED_OUT"
    assert made["req"].parked and made["stream"].parked


def test_client_id_change_drops_the_old_grant_without_revoking(run, domoticz):
    old = replace(live_tokens(), client_id_hash=auth.client_id_hash("PREVIOUS-CLIENT"))
    store(domoticz, tokens=old, auth_state="AUTHORIZED")
    made = run()
    plugin.onHeartbeat()
    assert "revoke" not in made["req"].kinds()
    assert made["req"].take("device_code")
    assert log_count(domoticz, "previous_app_not_revoked") == 1
    assert any(level == "status" and "previous_app_not_revoked" in m for level, m in domoticz._log)
    assert REFRESH not in log_text(domoticz)
    assert saved_state(domoticz).tokens is None


def test_restart_during_authorization_starts_a_fresh_flow(run, domoticz):
    store(domoticz, auth_state="AWAITING_USER")
    made = run()
    plugin.onHeartbeat()
    assert made["req"].take("device_code")
    assert plugin._s().auth_state == "UNAUTHORIZED"


# -- re-authorize -------------------------------------------------------------------------------


def test_reauthorize_keeps_tokens_until_the_new_grant(run):
    made = run()
    authorize(made)
    plugin.onCommand(planner.ACCOUNT_DEVICE, planner.ACCOUNT_ACTIONS, "Set Level", 10, "")
    assert made["req"].take("device_code")
    assert plugin._s().tokens.access_token == ACCESS


def test_reauthorize_from_authorized_stays_authorized(run, domoticz):
    made = run()
    authorize(made)
    req = made["req"]
    plugin.onCommand(planner.ACCOUNT_DEVICE, planner.ACCOUNT_ACTIONS, "Set Level", 10, "")
    fakes.push(req, fakes.result(req, req.take("device_code"), 200, device_code_body()))
    plugin.onHeartbeat()
    s = plugin._s()
    assert s.auth_state == "AUTHORIZED" and s.flow is not None
    assert "ABCD-EFGH" in account_unit(domoticz).sValue
    s.flow = replace(s.flow, expires_at=0.0)
    s.tokens = live_tokens(expires_in=10)
    plugin.onHeartbeat()
    assert s.auth_state == "AUTHORIZED" and s.flow is None
    assert "refresh" in req.kinds()  # the proactive refresh keeps running


def test_reauthorize_grant_replaces_tokens(run):
    made = run()
    authorize(made)
    plugin.onCommand(planner.ACCOUNT_DEVICE, planner.ACCOUNT_ACTIONS, "Set Level", 10, "")
    req = made["req"]
    fakes.push(req, fakes.result(req, req.take("device_code"), 200, device_code_body()))
    plugin.onHeartbeat()
    plugin._s().next_poll = 0
    approve(made, ACCESS2)
    assert plugin._s().tokens.access_token == ACCESS2
    assert req.token == ACCESS2


def test_reauthorize_is_refused_while_signed_out(run, domoticz):
    made = run(SignOut="true")
    plugin.onHeartbeat()
    plugin.onCommand(planner.ACCOUNT_DEVICE, planner.ACCOUNT_ACTIONS, "Set Level", 10, "")
    assert "device_code" not in made["req"].kinds()
    assert "Sign out" in log_text(domoticz)


def test_refresh_appliances_lists_again(run):
    made = run()
    authorize(made)
    discover(made)
    plugin.onCommand(planner.ACCOUNT_DEVICE, planner.ACCOUNT_ACTIONS, "Set Level", 20, "")
    assert "list" in made["req"].kinds()


def test_appliance_commands_are_refused_while_control_is_off(run, domoticz):
    made = run()
    authorize(made)
    discover(made)
    made["req"].jobs.clear()
    for unit in (2, 8, 9, 10, 12, 20, 25):
        plugin.onCommand(HA, unit, "Set Level", 10, "")
    assert made["req"].jobs == []
    assert log_count(domoticz, "command_refused") == 7
    assert "'Allow control' is off" in log_text(domoticz)
    assert HA not in log_text(domoticz)


def test_status_code_selector_commands_are_refused(run, domoticz):
    run(AllowControl="true")
    plugin.onCommand(HA, planner.UNIT_STATUS_CODE, "Set Level", 40, "")
    assert log_count(domoticz, "command_refused") == 1
    assert "only shows a value" in log_text(domoticz)


# -- discovery, model and devices ---------------------------------------------------------------


def test_discovery_creates_devices_and_starts_the_stream(run, domoticz):
    made = run()
    authorize(made)
    discover(made)
    assert made["stream"].token == ACCESS
    answer(made, "status", body=status_body())
    assert domoticz.Devices[HA].Units[planner.UNIT_STATE].sValue == "Running"
    code = domoticz.Devices[HA].Units[planner.UNIT_STATUS_CODE]
    assert (code.nValue, code.stored["Used"]) == (40, 0)
    assert made["req"].take("active").ha_id == HA


def test_fresh_install_creates_account_units_in_the_injected_devices(run, domoticz):
    # A fresh hardware entry injects an empty Devices table; created units must be looked up
    # in that same table, or every creation is reported as failed.
    assert domoticz.Devices == {}
    run()
    plugin.onHeartbeat()
    assert log_count(domoticz, "Could not create") == 0
    assert planner.ACCOUNT_DEVICE in domoticz.Devices
    names = plugin._s().state.auto_names
    assert names[f"{planner.ACCOUNT_DEVICE}:{planner.ACCOUNT_TEXT}"] == account_unit(domoticz).Name


def test_failed_listing_is_retried(run, clock):
    made = run()
    authorize(made)
    answer(made, "list", 0, category="network")
    assert "list" not in made["req"].kinds()
    clock.offset += plugin.RETRY_BACKOFF_FIRST_S
    plugin.onHeartbeat()
    assert "list" in made["req"].kinds()


def test_settings_answer_reaches_the_model_before_the_scheduler(run):
    made = run()
    authorize(made)
    discover(made)
    key = "BSH.Common.Setting.ChildLock"
    event(made, "NOTIFY", json.dumps({"haId": HA, "items": [{"key": key, "value": True}]}))
    plugin.onHeartbeat()
    body = json.dumps({"data": {"settings": [{"key": key, "value": False}]}}).encode()
    answer(made, "settings", body=body)
    app = plugin._s().model.appliances[HA]
    assert key not in app.cache.negative_keys
    assert app.settings[key].value is False


def test_unresolved_setting_is_negative_cached(run):
    made = run()
    authorize(made)
    discover(made)
    key = "BSH.Common.Setting.Mystery"
    event(made, "NOTIFY", json.dumps({"haId": HA, "items": [{"key": key, "value": 1}]}))
    plugin.onHeartbeat()
    answer(made, "settings", body=json.dumps({"data": {"settings": []}}).encode())
    assert key in plugin._s().model.appliances[HA].cache.negative_keys


def test_daily_refresh_runs_from_the_heartbeat(run):
    made = run()
    authorize(made)
    discover(made)
    plugin._s().beat = plugin.DAILY_CHECK_EVERY - 1
    plugin.onHeartbeat()
    assert "available" in made["req"].kinds()


def test_capped_appliances_are_logged_by_name(run, domoticz, monkeypatch):
    made = run()
    authorize(made)
    discover(made)
    monkeypatch.setattr(plugin._s().sched, "take_capped", lambda: [HA])
    plugin.onHeartbeat()
    assert "refetch_capped" in log_text(domoticz)
    assert HA not in log_text(domoticz)


def test_duplicate_names_are_logged_once(run, domoticz):
    made = run()
    authorize(made)
    discover(made, appliance(HA, "Oven"), appliance(HA2, "Oven", "Dishwasher"))
    plugin.onHeartbeat()
    plugin.onHeartbeat()
    assert log_count(domoticz, "duplicate_appliance_name") == 1


def test_account_visibility_changes_only_on_transitions(run, domoticz):
    made = run()
    authorize(made)
    unit = account_unit(domoticz)
    assert unit.stored["Used"] == 0
    unit.Used = 1
    unit.Update(UpdateProperties=True)  # the user shows it again by hand
    plugin.onHeartbeat()
    plugin.onHeartbeat()
    assert unit.stored["Used"] == 1


def test_forbidden_feature_is_logged_once(run, domoticz):
    made = run()
    authorize(made)
    discover(made)
    req = made["req"]
    settings = req.take("settings")
    for _ in range(2):
        fakes.push(req, fakes.result(req, settings, 403, b"", "forbidden", "insufficient_scope"))
        plugin.onHeartbeat()
    assert log_count(domoticz, "feature_refused") == 1


def test_offline_answer_marks_the_appliance_offline(run, domoticz):
    made = run()
    authorize(made)
    discover(made)
    answer(made, "status", 409, b"", "offline", "x")
    assert domoticz.Devices[HA].Units[planner.UNIT_STATE].sValue == "Offline"


def test_verbose_logs_each_event_item_with_its_whole_value(run, domoticz):
    made = run()
    authorize(made)
    discover(made)
    summary = {"water": "w" * 400}
    items = [
        {"key": "BSH.Common.Status.ProgramSessionSummary.Latest", "value": summary, "uri": HA},
        {"key": "BSH.Common.Status.DoorState", "value": "x", "displayvalue": "Open"},
    ]
    event(made, "STATUS", json.dumps({"haId": HA, "items": items}))
    plugin.onHeartbeat()
    lines = [m for _, m in domoticz._log if m.startswith("event ")]
    assert len(lines) == 2
    assert 'appliance="Oven" key="BSH.Common.Status.ProgramSessionSummary.Latest"' in lines[0]
    assert "w" * 400 in lines[0]
    assert lines[1].endswith('key="BSH.Common.Status.DoorState" value="x" display="Open"')
    assert HA not in log_text(domoticz)


def test_basic_debug_level_logs_no_event_payloads(run, domoticz):
    made = run(DebugLevel="1")
    authorize(made)
    discover(made)
    event(made, "STATUS", json.dumps({"haId": HA, "items": [{"key": "K.A", "value": 1}]}))
    plugin.onHeartbeat()
    assert log_count(domoticz, "event ") == 0


def test_offline_answer_then_stream_item_brings_the_appliance_back(run, domoticz):
    made = run()
    authorize(made)
    discover(made)
    answer(made, "status", 409, b"", "offline", "x")
    made["req"].jobs.clear()
    items = [{"key": "BSH.Common.Status.OperationState", "value": RUN}]
    event(made, "STATUS", json.dumps({"haId": HA, "items": items}))
    plugin.onHeartbeat()
    assert domoticz.Devices[HA].Units[planner.UNIT_STATE].sValue == "Running"
    assert made["req"].kinds() == ["status", "settings"]


def test_block_start_and_end_are_logged(run, domoticz, clock):
    made = run()
    authorize(made)
    plugin._s().budget.block(120)
    plugin.onHeartbeat()
    assert any(level == "status" and "api_blocked" in m for level, m in domoticz._log)
    clock.offset += 200
    plugin.onHeartbeat()
    assert any(level == "status" and "api_block_ended" in m for level, m in domoticz._log)


def test_depaired_appliance_stops_updating(run, domoticz):
    made = run()
    authorize(made)
    discover(made)
    answer(made, "status", body=status_body())
    event(made, "DEPAIRED")
    plugin.onHeartbeat()
    plugin.onHeartbeat()
    assert domoticz.Devices[HA].Units[planner.UNIT_STATE].sValue == "Running"
    assert log_count(domoticz, "appliance_removed") == 1


def test_disconnected_appliance_shows_offline(run, domoticz):
    made = run()
    authorize(made)
    discover(made)
    answer(made, "status", body=status_body())
    event(made, "DISCONNECTED")
    plugin.onHeartbeat()
    assert domoticz.Devices[HA].Units[planner.UNIT_STATE].sValue == "Offline"
    assert "appliance_offline" in log_text(domoticz)


def test_unhealthy_stream_marks_state_last_known(run, domoticz, clock):
    made = run()
    authorize(made)
    discover(made)
    answer(made, "status", body=status_body())
    unit = domoticz.Devices[HA].Units[planner.UNIT_STATE]
    for _ in range(3):  # a stream that keeps connecting and dropping never proves healthy
        stream_state(made, True)
        stream_state(made, False)
        clock.offset += 150
        plugin.onHeartbeat()
    assert unit.sValue == "Running (last known)"
    stream_state(made, True)
    stream_state(made, True, healthy=True)
    plugin.onHeartbeat()
    assert unit.sValue == "Running"


def test_stream_reconnect_resyncs(run):
    made = run()
    authorize(made)
    discover(made)
    answer(made, "status", body=status_body())
    made["req"].jobs.clear()
    stream_state(made, True)
    plugin.onHeartbeat()
    assert made["req"].jobs == []  # the first connect follows discovery
    stream_state(made, False)
    stream_state(made, True)
    plugin.onHeartbeat()
    assert "status" in made["req"].kinds()


# -- workers ------------------------------------------------------------------------------------


def test_only_the_dead_worker_is_restarted(run):
    made = run()
    authorize(made)
    old_req, stream = made["req"], made["stream"]
    old_req.state = "crashed"
    plugin.onHeartbeat()
    assert made["req"] is not old_req
    assert made["stream"] is stream
    assert len(made["clients"]) == 3
    assert made["req"].token == ACCESS
    assert "list" in made["req"].kinds()


def test_restart_resets_the_refresh_in_flight(run):
    made = run()
    authorize(made)
    stream = made["stream"]
    fakes.push(stream, workers.NeedsRefresh(stream.generation))
    plugin.onHeartbeat()
    made["req"].state = "crashed"
    plugin.onHeartbeat()
    plugin.onHeartbeat()
    assert "refresh" in made["req"].kinds()


def test_restarts_slow_to_once_an_hour(run, domoticz, clock):
    made = run()
    for _ in range(plugin.WORKER_RESTARTS_PER_HOUR):
        dead = made["stream"]
        dead.state = "crashed"
        plugin.onHeartbeat()
        assert made["stream"] is not dead
    dead = made["stream"]
    dead.state = "crashed"
    plugin.onHeartbeat()
    assert made["stream"] is dead
    assert plugin._s().workers_down
    assert "Workers stopped" in account_unit(domoticz).sValue
    assert log_count(domoticz, "workers_down") == 1
    clock.offset += plugin.SLOW_RESTART_INTERVAL_S
    plugin.onHeartbeat()
    assert made["stream"] is not dead
    assert not plugin._s().workers_down


def test_on_stop_persists(run, domoticz):
    made = run()
    plugin.onStop()
    assert made["req"].stopped and made["stream"].stopped
    assert persistence.STATE_KEY in domoticz.Configuration()


def test_on_stop_saves_the_ledger(run, domoticz):
    made = run()
    plugin.onHeartbeat()
    plugin._s().budget.acquire("auth")
    plugin.onStop()
    assert saved_state(domoticz).ledger["auth"]
    assert made["req"].stopped


# -- fix round 1 --------------------------------------------------------------------------------

JWT = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9." + "eyJzdWIiOiJ0ZXN0In0" * 40 + ".c2lnbmF0dXJl-_x"


def test_redaction_survives_truncation_of_long_values(run, domoticz):
    made = run()
    start_flow(made)
    approve(made, JWT)
    discover(made)
    req = made["req"]
    fields = {
        "a": "y" * 195 + HA,  # the 200-character cut falls inside the haId
        "b": "z" * 190 + ACCESS,
        "c": "w" * 150 + JWT,
        "d": JWT,
    }
    plugin._s().remember(ACCESS)
    fakes.push(req, workers.LogRecord(req.generation, "status", "probe", fields))
    plugin.onHeartbeat()
    text = log_text(domoticz)
    assert "probe" in text
    for secret in (HA, ACCESS, JWT):
        assert secret[:10] not in text


def test_worker_keys_are_quoted_and_cleaned_once(run, domoticz):
    made = run()
    req = made["req"]
    record = {"key": 'SDK.Error.Weird"\nkey'}
    fakes.push(req, workers.LogRecord(req.generation, "status", "request", record))
    plugin.onHeartbeat()
    [line] = [m for _, m in domoticz._log if m.startswith("request ")]
    assert line == 'request key="SDK.Error.Weird\\" key"'


def test_existing_device_ids_are_redacted_from_the_start(run, domoticz):
    unit = domoticz.Unit(
        Name=f"{HA} Program", DeviceID=HA, Unit=25, TypeName="Selector Switch", Switchtype=18
    )
    unit.Create()
    run()
    assert "selector_unmapped" in log_text(domoticz)
    assert HA not in log_text(domoticz)


def test_listing_retries_back_off_and_reset(run, clock):
    made = run()
    authorize(made)
    attempts = 0
    for _ in range(60):  # an hour of heartbeats a minute apart, every listing failing
        if "list" in made["req"].kinds():
            attempts += 1
            answer(made, "list", 0, category="network")
        clock.offset += 60
        plugin.onHeartbeat()
    assert attempts <= 7
    while "list" not in made["req"].kinds():
        clock.offset += 60
        plugin.onHeartbeat()
    answer(made, "list", body=listing())
    plugin.onCommand(planner.ACCOUNT_DEVICE, planner.ACCOUNT_ACTIONS, "Set Level", 20, "")
    answer(made, "list", 0, category="network")
    clock.offset += plugin.RETRY_BACKOFF_FIRST_S
    plugin.onHeartbeat()
    assert "list" in made["req"].kinds()  # back to the first delay after a success


def test_device_code_retries_back_off(run, clock):
    made = run()
    plugin.onHeartbeat()
    attempts = 0
    for _ in range(60):
        if "device_code" in made["req"].kinds():
            attempts += 1
            answer(made, "device_code", 503)
        clock.offset += 60
        plugin.onHeartbeat()
    assert attempts <= 7


def test_failed_device_code_start_expires_with_reason(run, domoticz):
    made = run()
    plugin.onHeartbeat()
    answer(made, "device_code", 400, oauth_error("invalid_client"), "oauth", "invalid_client")
    assert plugin._s().auth_state == "EXPIRED_CODE"
    assert "invalid_client" in account_unit(domoticz).sValue
    assert any(level == "error" and "authorization_start_failed" in m for level, m in domoticz._log)


def test_poll_error_names_the_code_on_the_account_device(run, domoticz):
    made = run()
    start_flow(made)
    plugin.onHeartbeat()
    body = oauth_error("invalid_request")
    answer(made, "token_poll", 400, body, "oauth", "invalid_request")
    assert plugin._s().auth_state == "EXPIRED_CODE"
    assert "invalid_request" in account_unit(domoticz).sValue


def refresh_answer(made, status, code):
    req = made["req"]
    plugin._s().tokens = live_tokens(expires_in=10)
    plugin.onHeartbeat()
    fakes.push(
        req, fakes.result(req, req.take("refresh"), status, oauth_error(code), "oauth", code)
    )
    plugin.onHeartbeat()


def test_rejected_client_needs_user_action(run, domoticz, clock):
    made = run()
    authorize(made)
    refresh_answer(made, 401, "invalid_client")
    s = plugin._s()
    assert s.auth_state == "NEEDS_USER_ACTION"
    assert "Client ID" in account_unit(domoticz).sValue
    assert account_unit(domoticz).stored["Used"] == 1
    for _ in range(20):
        clock.offset += 60
        plugin.onHeartbeat()
    assert "refresh" not in made["req"].kinds()
    assert log_count(domoticz, "authorization_lost") == 1


def test_unmapped_refresh_refusal_needs_user_action(run, domoticz):
    made = run()
    authorize(made)
    refresh_answer(made, 400, "invalid_scope")
    assert plugin._s().auth_state == "NEEDS_USER_ACTION"
    assert "invalid_scope" in account_unit(domoticz).sValue
    assert any(level == "error" and "authorization_lost" in m for level, m in domoticz._log)


def test_transient_refresh_failure_retries_later(run, clock):
    made = run()
    authorize(made)
    refresh_answer(made, 503, "unknown")
    assert plugin._s().auth_state == "AUTHORIZED"
    assert "refresh" not in made["req"].kinds()
    clock.offset += plugin.RETRY_LATER_S
    plugin.onHeartbeat()
    assert "refresh" in made["req"].kinds()


def test_transient_refresh_failures_back_off_and_reset_on_success(run, clock):
    made = run()
    authorize(made)
    req = made["req"]
    plugin._s().tokens = live_tokens(expires_in=10)
    delays = []
    for _ in range(7):
        plugin.onHeartbeat()
        fakes.push(req, fakes.result(req, req.take("refresh"), 503, b"", "server", ""))
        plugin.onHeartbeat()
        delays.append(round(plugin._s().refresh_next - clock()))
        clock.offset += delays[-1]
    first, cap = plugin.RETRY_LATER_S, plugin.RETRY_BACKOFF_MAX_S
    assert delays == [first, 2 * first, 4 * first, 8 * first, cap, cap, cap]
    plugin.onHeartbeat()
    fakes.push(req, fakes.result(req, req.take("refresh"), 200, token_body(ACCESS2)))
    plugin.onHeartbeat()
    plugin._s().tokens = live_tokens(expires_in=10)
    plugin.onHeartbeat()
    fakes.push(req, fakes.result(req, req.take("refresh"), 503, b"", "server", ""))
    plugin.onHeartbeat()
    assert round(plugin._s().refresh_next - clock()) == first


def test_healthy_reconnect_never_flashes_last_known(run, domoticz, clock):
    made = run()
    authorize(made)
    discover(made)
    answer(made, "status", body=status_body())
    unit = domoticz.Devices[HA].Units[planner.UNIT_STATE]
    stream_state(made, True, healthy=True)
    plugin.onHeartbeat()
    stream_state(made, False)  # a healthy stream drops
    plugin.onHeartbeat()
    clock.offset += 12  # the first reconnect backoff, at its jitter maximum
    stream_state(made, True)
    plugin.onHeartbeat()
    clock.offset += 368  # about 380 s after the drop, the keep-alive that proves it still due
    plugin.onHeartbeat()
    assert not plugin._stream_stale()
    assert unit.sValue == "Running"
    stream_state(made, True, healthy=True)
    plugin.onHeartbeat()
    assert unit.sValue == "Running"


def test_last_known_clears_on_the_first_event_after_a_reconnect(run, domoticz, clock):
    made = run()
    authorize(made)
    discover(made)
    answer(made, "status", body=status_body())
    unit = domoticz.Devices[HA].Units[planner.UNIT_STATE]
    stream_state(made, True, live=True)
    stream_state(made, False)
    plugin.onHeartbeat()
    clock.offset += plugin.STREAM_STALE_AFTER_S + 1
    plugin.onHeartbeat()
    assert unit.sValue == "Running (last known)"
    stream_state(made, True)
    plugin.onHeartbeat()
    assert unit.sValue == "Running (last known)"  # connected, but nothing received yet
    stream_state(made, True, live=True)
    plugin.onHeartbeat()
    assert unit.sValue == "Running"


def test_stream_that_never_proves_healthy_goes_stale(run, domoticz, clock):
    made = run()
    authorize(made)
    discover(made)
    answer(made, "status", body=status_body())
    stream_state(made, True)
    clock.offset += plugin.STREAM_STALE_AFTER_S + 1
    plugin.onHeartbeat()
    assert plugin._stream_stale()
    assert domoticz.Devices[HA].Units[planner.UNIT_STATE].sValue == "Running (last known)"


def test_restarted_stream_must_prove_itself(run):
    made = run()
    authorize(made)
    discover(made)
    stream_state(made, True, healthy=True)
    plugin.onHeartbeat()
    assert plugin._s().not_live_since is None
    made["stream"].state = "crashed"
    plugin.onHeartbeat()
    assert plugin._s().not_live_since is not None


def stream_refreshed(made):
    req, stream = made["req"], made["stream"]
    fakes.push(stream, workers.NeedsRefresh(stream.generation))
    plugin.onHeartbeat()
    fakes.push(req, fakes.result(req, req.take("refresh"), 200, token_body(ACCESS2)))
    plugin.onHeartbeat()


def test_stream_401_straight_after_refresh_needs_reauth(run):
    made = run()
    authorize(made)
    discover(made)
    stream_refreshed(made)
    stream = made["stream"]
    fakes.push(stream, workers.NeedsRefresh(stream.generation))
    plugin.onHeartbeat()
    assert plugin._s().auth_state == "NEEDS_REAUTH"
    assert "refresh" not in made["req"].kinds()


def test_stream_401_after_an_event_refreshes_again(run):
    made = run()
    authorize(made)
    discover(made)
    stream_refreshed(made)
    stream_state(made, True)
    stream = made["stream"]
    fakes.push(stream, workers.NeedsRefresh(stream.generation))
    plugin.onHeartbeat()
    assert plugin._s().auth_state == "AUTHORIZED"
    assert "refresh" in made["req"].kinds()


def test_reauthorize_from_needs_reauth_returns_there(run, domoticz):
    store(domoticz, tokens=live_tokens(), auth_state="NEEDS_REAUTH")
    made = run()
    plugin.onHeartbeat()
    req = made["req"]
    assert req.parked
    plugin.onCommand(planner.ACCOUNT_DEVICE, planner.ACCOUNT_ACTIONS, "Set Level", 10, "")
    assert not req.parked  # spec 8.5
    answer(made, "device_code", body=device_code_body())
    s = plugin._s()
    assert s.auth_state == "NEEDS_REAUTH" and s.flow is not None
    assert "ABCD-EFGH" in account_unit(domoticz).sValue
    s.flow = replace(s.flow, expires_at=0.0)
    plugin.onHeartbeat()
    assert s.auth_state == "NEEDS_REAUTH" and s.flow is None
    assert req.parked


def test_denied_reauthorize_while_authorized_keeps_the_note(run, domoticz):
    made = run()
    authorize(made)
    plugin.onCommand(planner.ACCOUNT_DEVICE, planner.ACCOUNT_ACTIONS, "Set Level", 10, "")
    answer(made, "device_code", body=device_code_body())
    plugin._s().next_poll = 0
    plugin.onHeartbeat()
    answer(made, "token_poll", 403, oauth_error("access_denied"), "oauth", "access_denied")
    assert plugin._s().auth_state == "AUTHORIZED"
    assert "denied" in account_unit(domoticz).sValue


def test_appliance_offline_at_discovery_fills_after_connected(run, domoticz):
    made = run()
    authorize(made)
    discover(made, appliance(connected=False))
    assert "status" not in made["req"].kinds()
    assert domoticz.Devices[HA].Units[planner.UNIT_STATE].sValue == "Offline"
    event(made, "CONNECTED")
    plugin.onHeartbeat()
    answer(made, "status", body=status_body())
    assert domoticz.Devices[HA].Units[planner.UNIT_STATE].sValue == "Running"


def test_due_tokens_refresh_before_the_first_listing(run, domoticz):
    store(domoticz, tokens=live_tokens(expires_in=10), auth_state="AUTHORIZED")
    made = run()
    plugin.onHeartbeat()
    req = made["req"]
    assert req.kinds() == ["refresh"]
    fakes.push(req, fakes.result(req, req.take("refresh"), 200, token_body(ACCESS2)))
    plugin.onHeartbeat()
    assert req.kinds() == ["list"]
    assert req.token == ACCESS2


def test_revoke_failure_is_logged_once_and_not_repeated(run, domoticz):
    store(domoticz, tokens=live_tokens(), auth_state="AUTHORIZED")
    made = run(SignOut="true")
    plugin.onHeartbeat()
    answer(made, "revoke", 400, oauth_error("invalid_request"), "oauth", "invalid_request")
    plugin.onHeartbeat()
    assert "revoke" not in made["req"].kinds()
    assert log_count(domoticz, "revoke_failed") == 1


def test_revoke_refusal_is_logged_and_not_repeated(run, domoticz):
    store(domoticz, tokens=live_tokens(), auth_state="AUTHORIZED")
    made = run(SignOut="true")
    plugin.onHeartbeat()
    req = made["req"]
    fakes.push(req, workers.Refused(req.generation, req.take("revoke"), "ceiling"))
    plugin.onHeartbeat()
    plugin.onHeartbeat()
    assert "revoke" not in req.kinds()
    assert log_count(domoticz, "revoke_failed") == 1


# -- fix round 2 --------------------------------------------------------------------------------


def refresh_reply(made, status, body, category="", key=""):
    req = made["req"]
    plugin._s().tokens = live_tokens(expires_in=10)
    plugin.onHeartbeat()
    fakes.push(req, fakes.result(req, req.take("refresh"), status, body, category, key))
    plugin.onHeartbeat()


@pytest.mark.parametrize(
    ("status", "body"),
    [(407, b"<html>proxy</html>"), (403, b"<html>captive portal</html>"), (204, b""), (302, b"")],
)
def test_token_answers_without_an_oauth_error_are_transient(run, clock, status, body):
    made = run()
    authorize(made)
    refresh_reply(made, status, body)
    assert plugin._s().auth_state == "AUTHORIZED"
    clock.offset += plugin.RETRY_LATER_S
    plugin.onHeartbeat()
    assert "refresh" in made["req"].kinds()


@pytest.mark.parametrize(
    ("code", "state"), [("invalid_grant", "NEEDS_REAUTH"), ("invalid_client", "NEEDS_USER_ACTION")]
)
def test_oauth_refusals_lose_authorization(run, code, state):
    made = run()
    authorize(made)
    refresh_reply(made, 400, oauth_error(code), "oauth", code)
    assert plugin._s().auth_state == state


def test_stream_401_for_a_replaced_token_keeps_authorization(run):
    made = run()
    authorize(made)
    discover(made)  # the stream opens with ACCESS
    req, stream = made["req"], made["stream"]
    plugin._request_refresh()  # a REST 401 refreshes while that open is in flight
    fakes.push(req, fakes.result(req, req.take("refresh"), 200, token_body(ACCESS2)))
    plugin.onHeartbeat()
    fakes.push(stream, workers.NeedsRefresh(stream.generation, current=False))
    plugin.onHeartbeat()
    assert plugin._s().auth_state == "AUTHORIZED"
    assert "refresh" not in req.kinds()


def test_empty_device_ids_never_reach_the_redactor(run, domoticz):
    domoticz.Devices[""] = type("Dev", (), {"Units": {}, "DeviceID": ""})()
    domoticz.Devices["a"] = type("Dev", (), {"Units": {}, "DeviceID": "a"})()
    run(DomoticzVersion="2026.3")
    assert plugin._s().ha_ids == set()
    assert "domoticz_version" in log_text(domoticz)
    assert "recommended" in log_text(domoticz)


def test_new_code_while_authorized_clears_an_old_note(run, domoticz):
    made = run()
    authorize(made)
    plugin.onCommand(planner.ACCOUNT_DEVICE, planner.ACCOUNT_ACTIONS, "Set Level", 10, "")
    answer(made, "device_code", body=device_code_body())
    plugin._s().next_poll = 0
    plugin.onHeartbeat()
    answer(made, "token_poll", 403, oauth_error("access_denied"), "oauth", "access_denied")
    assert "denied" in account_unit(domoticz).sValue
    plugin.onCommand(planner.ACCOUNT_DEVICE, planner.ACCOUNT_ACTIONS, "Set Level", 10, "")
    answer(made, "device_code", body=device_code_body())
    assert "denied" not in account_unit(domoticz).sValue


def test_grant_and_loss_clear_the_fresh_stream_token(run):
    made = run()
    authorize(made)
    discover(made)
    stream_refreshed(made)
    s = plugin._s()
    assert s.stream_token_fresh
    s.tokens = live_tokens(expires_in=10)
    plugin.onHeartbeat()
    req = made["req"]
    fakes.push(req, fakes.result(req, req.take("refresh"), 400, oauth_error("invalid_grant")))
    plugin.onHeartbeat()
    assert s.auth_state == "NEEDS_REAUTH" and not s.stream_token_fresh
    s.stream_token_fresh = True
    plugin.onCommand(planner.ACCOUNT_DEVICE, planner.ACCOUNT_ACTIONS, "Set Level", 10, "")
    answer(made, "device_code", body=device_code_body())
    s.next_poll = 0
    approve(made, ACCESS)
    assert s.auth_state == "AUTHORIZED" and not s.stream_token_fresh


# -- names across a restart ---------------------------------------------------------------------

DURATION = "BSH.Common.Option.Duration"
PROGRAM = "Cooking.Oven.Program.HeatingMode.HotAir"


def discover_programs(made, option_name, option_key=DURATION):
    """Discovery of an idle oven down to one program detail, whose option Home Connect names."""
    discover(made)
    answer(made, "status", body=status_body("BSH.Common.EnumType.OperationState.Ready"))
    programs = {"data": {"programs": [{"key": PROGRAM, "name": "Hete lucht"}]}}
    answer(made, "available", body=json.dumps(programs).encode())
    option = {"key": option_key, "name": option_name, "type": "Int", "unit": "seconds"}
    detail = {"data": {"key": PROGRAM, "options": [option]}}
    answer(made, "program_detail", body=json.dumps(detail).encode())


def unit_names(domoticz):
    return {n: u.Name for n, u in domoticz.Devices[HA].Units.items()}


def test_home_connect_names_survive_a_restart(run, domoticz):
    made = run(DeviceLanguage="nl")
    authorize(made)
    # A key the catalog does not name, so the unit carries Home Connect's name.
    discover_programs(made, "Pas de kooktijd aan", "Cooking.Oven.Option.VendorCookTime")
    before = unit_names(domoticz)
    assert any("Pas de kooktijd aan" in name for name in before.values())
    plugin.onStop()
    renames = {n: len(u.updates) for n, u in domoticz.Devices[HA].Units.items()}
    made = run(DeviceLanguage="nl")
    plugin.onHeartbeat()
    discover(made)  # the restart lists again; no answer carrying names has arrived yet
    assert unit_names(domoticz) == before
    for number, unit in domoticz.Devices[HA].Units.items():
        new_updates = unit.updates[renames[number] :]
        assert not any(kw.get("UpdateProperties") for kw in new_updates), number


def test_persisted_names_never_carry_a_ha_id(run, domoticz):
    made = run()
    authorize(made)
    discover_programs(made, f"Duration {HA}")
    plugin.onStop()
    cache = saved_state(domoticz).caches[HA]
    assert cache.names  # the program name is kept
    for key, label in {**cache.names, **cache.value_names}.items():
        assert HA not in key and HA not in label


# -- clickable links on the account card ----------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "nl"])
def test_reauthorize_link_is_clickable_while_authorized(run, domoticz, lang):
    made = run(DeviceLanguage=lang)
    authorize(made)
    plugin.onCommand(planner.ACCOUNT_DEVICE, planner.ACCOUNT_ACTIONS, "Set Level", 10, "")
    answer(made, "device_code", body=device_code_body())
    svalue = account_unit(domoticz).sValue
    assert svalue.count("<a ") == 1 and svalue.count("</a>") == 1
    assert f'href="{LINK}"' in svalue and "ABCD-EFGH" in svalue


@pytest.mark.parametrize("state", ["NEEDS_REAUTH", "NEEDS_USER_ACTION"])
@pytest.mark.parametrize("lang", ["en", "nl"])
def test_reauthorize_link_is_clickable_after_a_lost_authorization(run, domoticz, state, lang):
    store(domoticz, tokens=live_tokens(), auth_state=state)
    made = run(DeviceLanguage=lang)
    plugin.onHeartbeat()
    plugin.onCommand(planner.ACCOUNT_DEVICE, planner.ACCOUNT_ACTIONS, "Set Level", 10, "")
    answer(made, "device_code", body=device_code_body())
    svalue = account_unit(domoticz).sValue
    assert plugin._s().auth_state == state
    assert svalue.count("<a ") == 1 and svalue.count("</a>") == 1
    assert f'href="{LINK}"' in svalue and "ABCD-EFGH" in svalue


# -- polling through non-OAuth answers ----------------------------------------------------------


def pending_reauthorize(made):
    authorize(made)
    plugin.onCommand(planner.ACCOUNT_DEVICE, planner.ACCOUNT_ACTIONS, "Set Level", 10, "")
    answer(made, "device_code", body=device_code_body())
    plugin._s().next_poll = 0
    plugin.onHeartbeat()


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (407, b"<html>proxy</html>"),
        (200, b""),
        (400, b"\x00garbled not json"),
        (403, b"<html>captive portal</html>"),
        (204, b""),
        (0, b""),
    ],
)
def test_non_oauth_poll_answers_keep_polling(run, domoticz, clock, status, body):
    made = run()
    pending_reauthorize(made)
    answer(made, "token_poll", status, body)
    s = plugin._s()
    assert s.flow is not None and s.auth_state == "AUTHORIZED"
    assert "sign_in_ended" not in log_text(domoticz)
    clock.offset += s.flow.interval
    plugin.onHeartbeat()
    assert "token_poll" in made["req"].kinds()


def test_coded_denial_still_ends_the_flow(run, domoticz):
    made = run()
    pending_reauthorize(made)
    answer(made, "token_poll", 403, oauth_error("access_denied"), "oauth", "access_denied")
    assert plugin._s().flow is None
    assert 'outcome="denied"' in log_text(domoticz)


def test_refresh_success_clears_a_stale_flow_note(run, domoticz):
    made = run()
    pending_reauthorize(made)
    answer(made, "token_poll", 400, oauth_error("invalid_request"), "oauth", "invalid_request")
    assert "invalid_request" in account_unit(domoticz).sValue
    plugin._s().tokens = live_tokens(expires_in=10)
    plugin.onHeartbeat()
    answer(made, "refresh", body=token_body(ACCESS2))
    assert "invalid_request" not in account_unit(domoticz).sValue


def test_same_batch_offline_then_online_logs_nothing(run, domoticz):
    made = run()
    authorize(made)
    discover(made)
    answer(made, "status", body=status_body())
    event(made, "DISCONNECTED")
    event(made, "CONNECTED")
    plugin.onHeartbeat()
    assert "appliance_offline" not in log_text(domoticz)
    assert "appliance_online" not in log_text(domoticz)
    assert domoticz.Devices[HA].Units[planner.UNIT_STATE].sValue != "Offline"


def test_lasting_offline_is_logged_then_online(run, domoticz):
    made = run()
    authorize(made)
    discover(made)
    answer(made, "status", body=status_body())
    event(made, "DISCONNECTED")
    plugin.onHeartbeat()
    assert log_count(domoticz, "appliance_offline") == 1
    event(made, "CONNECTED")
    plugin.onHeartbeat()
    assert log_count(domoticz, "appliance_online") == 1


def test_missed_connected_live_item_logs_online_and_refetches(run, domoticz):
    made = run()
    authorize(made)
    discover(made)
    answer(made, "status", body=status_body())
    event(made, "DISCONNECTED")
    plugin.onHeartbeat()
    made["req"].jobs.clear()
    items = [{"key": "BSH.Common.Status.OperationState", "value": RUN}]
    event(made, "STATUS", json.dumps({"haId": HA, "items": items}))
    plugin.onHeartbeat()
    assert domoticz.Devices[HA].Units[planner.UNIT_STATE].sValue == "Running"
    assert log_count(domoticz, "appliance_online") == 1
    assert "status" in made["req"].kinds()


# -- revoke on sign-out -------------------------------------------------------------------------


class RecordingClient:
    """An HCClient stand-in that answers every request with one canned response."""

    def __init__(self, status, body=b""):
        self.status, self.body = status, body
        self.calls = []

    def abort(self):
        pass

    def request(self, method, path, **kw):
        self.calls.append((method, path, dict(kw.get("form") or {}), kw.get("token")))
        return hc_client.Response(self.status, {}, self.body, 3)


def signed_out_with_a_real_request_worker(domoticz, monkeypatch, client):
    store(domoticz, tokens=live_tokens(), auth_state="AUTHORIZED")

    def req(client_, b, results, generation, lang):
        return workers.RequestWorker(
            client, b, results, generation=generation, accept_language=lang
        )

    def stream(client_, b, results, generation, lang):
        return fakes.FakeStreamWorker(client_, b, results, generation, lang)

    monkeypatch.setattr(plugin, "_new_request_worker", req)
    monkeypatch.setattr(plugin, "_new_stream_worker", stream)
    monkeypatch.setattr(plugin, "_new_client", lambda: fakes.NoClient())
    plugin.Parameters = {"HardwareID": "1", "Username": CID, "Password": SECRET, "SignOut": "true"}
    plugin.Devices = domoticz.Devices
    plugin.onStart()
    plugin.onHeartbeat()
    deadline = time.monotonic() + 3
    while not plugin._s().results.qsize() and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    plugin.onHeartbeat()
    plugin.onStop()


def test_sign_out_sends_the_revoke_with_the_client_id(domoticz, monkeypatch):
    client = RecordingClient(200)
    signed_out_with_a_real_request_worker(domoticz, monkeypatch, client)
    [(method, path, form, token)] = client.calls
    assert (method, path, token) == ("POST", hc_client.REVOKE_PATH, None)
    assert form == {
        "token": REFRESH,
        "token_type_hint": "refresh_token",
        "client_id": CID,
        "client_secret": SECRET,
    }
    text = log_text(domoticz)
    assert "revoke_failed" not in text
    assert any(
        level == "status" and m.startswith("revoked status=200") for level, m in domoticz._log
    )


def test_failed_revoke_logs_its_status_and_key(domoticz, monkeypatch):
    client = RecordingClient(400, oauth_error("invalid_client"))
    signed_out_with_a_real_request_worker(domoticz, monkeypatch, client)
    [line] = [m for level, m in domoticz._log if "revoke_failed" in m]
    assert 'status=400 key="invalid_client"' in line
    for secret in (REFRESH, SECRET, ACCESS):
        assert secret not in log_text(domoticz)


# -- control -------------------------------------------------------------------------------------

OVEN_PROGRAM = "Cooking.Oven.Program.HeatingMode.HotAir"
SETPOINT = "Cooking.Oven.Option.SetpointTemperature"
START_IN = "BSH.Common.Option.StartInRelative"
OPERATION = "BSH.Common.Status.OperationState"
STATE = "BSH.Common.EnumType.OperationState."
START_LIMITS = {"min": 0, "max": 86340, "stepsize": 60}
START = planner.ACTION_LEVELS["start"]
STOP = planner.ACTION_LEVELS["stop"]
MODE = "Cooking.Oven.Option.HeatupMode"


def items(*pairs):
    return json.dumps({"haId": HA, "items": [{"key": k, "value": v} for k, v in pairs]})


def selected_body(temperature=180):
    options = [{"key": SETPOINT, "value": temperature, "unit": "°C"}]
    return json.dumps({"data": {"key": OVEN_PROGRAM, "options": options}}).encode()


def control_ready(run, extra_options=(), start_limits=START_LIMITS, **params):
    """An authorized idle oven with control on, one program selected and its details known."""
    made = run(AllowControl="true", **params)
    authorize(made)
    discover(made)
    status = [
        {"key": OPERATION, "value": STATE + "Ready"},
        {"key": "BSH.Common.Status.RemoteControlStartAllowed", "value": True},
    ]
    answer(made, "status", body=json.dumps({"data": {"status": status}}).encode())
    programs = {"data": {"programs": [{"key": OVEN_PROGRAM, "name": "Hete lucht"}]}}
    answer(made, "available", body=json.dumps(programs).encode())
    setpoint = {
        "key": SETPOINT,
        "type": "Double",
        "unit": "°C",
        "constraints": {"min": 30, "max": 250, "stepsize": 5},
    }
    start_in = {"key": START_IN, "type": "Int", "unit": "seconds", "constraints": start_limits}
    options = [setpoint, start_in, *extra_options]
    detail = {"data": {"key": OVEN_PROGRAM, "options": options}}
    answer(made, "program_detail", body=json.dumps(detail).encode())
    answer(made, "selected", body=selected_body())
    made["req"].jobs.clear()
    return made


def setpoint_unit():
    return plugin._s().state.unit_alloc[HA][SETPOINT]


def reply(made, kind, status=204, body=b"", category="", key=""):
    req = made["req"]
    job = req.take(kind)
    fakes.push(req, fakes.result(req, job, status, body, category, key))
    plugin.onHeartbeat()
    return job


def test_select_temperature_and_start_go_out_one_at_a_time_in_order(run, clock):
    made = control_ready(run)
    req = made["req"]
    plugin.onCommand(HA, planner.UNIT_PROGRAM_SELECT, "Set Level", 10, "")
    plugin.onCommand(HA, setpoint_unit(), "Set Level", 200.0, "")
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", START, "")
    assert req.kinds() == ["command"]
    select = reply(made, "command")
    assert (select.method, select.body) == ("PUT", {"data": {"key": OVEN_PROGRAM}})
    reply(made, "verify", 200, selected_body())  # a selection is confirmed by its own GET
    clock.offset += coordinator.SETPOINT_DEBOUNCE_S
    plugin.onHeartbeat()
    temperature = reply(made, "command")
    assert temperature.body == {"data": {"key": SETPOINT, "value": 200, "unit": "°C"}}
    assert req.kinds() == ["verify"]  # the waiting Start asks for the new value at once
    event(made, "NOTIFY", items((SETPOINT, 200)))
    plugin.onHeartbeat()
    start = req.take("command")
    assert start.path.endswith("/programs/active") and start.limit == "start"
    expected = {"data": {"key": OVEN_PROGRAM, "options": [{"key": SETPOINT, "value": 200}]}}
    assert start.body == expected


def test_a_failed_temperature_change_blocks_the_start_behind_it(run, domoticz, clock):
    made = control_ready(run)
    plugin.onCommand(HA, setpoint_unit(), "Set Level", 200.0, "")
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", START, "")
    clock.offset += coordinator.SETPOINT_DEBOUNCE_S
    plugin.onHeartbeat()
    error = {"error": {"key": "SDK.Error.InvalidOptionValue", "description": "Value not allowed"}}
    body = json.dumps(error).encode()
    reply(made, "command", 409, body, "refused_validation", "SDK.Error.InvalidOptionValue")
    assert "command" not in made["req"].kinds()
    assert "Start not sent" in log_text(domoticz)
    assert "Value not allowed" in log_text(domoticz)


def test_stop_cancels_a_start_that_was_not_sent(run, domoticz, clock):
    made = control_ready(run)
    plugin.onCommand(HA, setpoint_unit(), "Set Level", 200.0, "")
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", START, "")
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", STOP, "")
    assert "Cancelled by Stop" in log_text(domoticz)
    clock.offset += coordinator.SETPOINT_DEBOUNCE_S
    plugin.onHeartbeat()
    assert reply(made, "command").body["data"]["key"] == SETPOINT
    event(made, "NOTIFY", items((SETPOINT, 200)))
    plugin.onHeartbeat()
    assert "command" not in made["req"].kinds()


def test_a_start_without_an_answer_is_checked_once_and_never_resent(run, domoticz):
    made = control_ready(run)
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", START, "")
    reply(made, "command", 0, category="network")
    check = reply(made, "verify", 200, status_body(STATE + "Ready"))
    assert (check.method, check.key) == ("GET", "status")
    assert made["req"].kinds() == []
    assert "did not start" in log_text(domoticz)


def test_the_commanded_value_shows_at_once_and_reverts_on_a_refusal(run, domoticz, clock):
    made = control_ready(run)
    unit = domoticz.Devices[HA].Units[setpoint_unit()]
    assert unit.sValue == "180.0"
    plugin.onCommand(HA, setpoint_unit(), "Set Level", 200.0, "")
    assert unit.sValue == "200.0"
    clock.offset += coordinator.SETPOINT_DEBOUNCE_S
    plugin.onHeartbeat()
    reply(made, "command", 409, b"", "refused_validation", "SDK.Error.InvalidOptionValue")
    assert unit.sValue == "180.0"


def test_heartbeat_runs_fast_only_while_commands_are_pending(run, domoticz):
    made = control_ready(run)
    assert domoticz._heartbeat == plugin.HEARTBEAT_S
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", START, "")
    assert domoticz._heartbeat == plugin.FAST_HEARTBEAT_S
    reply(made, "command")
    event(made, "STATUS", items((OPERATION, STATE + "Run")))
    plugin.onHeartbeat()
    assert domoticz._heartbeat == plugin.HEARTBEAT_S


def test_a_held_delayed_start_goes_with_start_as_seconds_from_now(run, monkeypatch):
    made = control_ready(run)
    now = time.time()
    monkeypatch.setattr(plugin, "_wall", lambda: now)
    level = planner.delay_level(17 * 60)
    plugin.onCommand(HA, planner.UNIT_DELAYED_START, "Set Level", level, "")
    assert plugin._s().state.delayed_start[HA] == 17 * 60
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", START, "")
    start = made["req"].take("command")
    expected = control.delay_seconds(17 * 60, START_LIMITS, now, "en")
    assert isinstance(expected, control.Delay)
    assert {"key": START_IN, "value": expected.seconds} in start.body["data"]["options"]


def test_the_held_time_is_spent_when_the_delayed_program_starts(run):
    made = control_ready(run)
    s = plugin._s()
    s.state.delayed_start[HA] = 17 * 60
    event(made, "STATUS", items((OPERATION, STATE + "DelayedStart")))
    plugin.onHeartbeat()
    assert HA in s.state.delayed_start
    event(made, "STATUS", items((OPERATION, STATE + "Run")))
    plugin.onHeartbeat()
    assert HA not in s.state.delayed_start


def test_a_start_is_checked_again_when_it_is_sent(run, domoticz, clock):
    made = control_ready(run)
    plugin.onCommand(HA, setpoint_unit(), "Set Level", 200.0, "")
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", START, "")
    event(made, "STATUS", items(("BSH.Common.Status.RemoteControlStartAllowed", False)))
    clock.offset += coordinator.SETPOINT_DEBOUNCE_S
    plugin.onHeartbeat()
    reply(made, "command")
    event(made, "NOTIFY", items((SETPOINT, 200)))
    plugin.onHeartbeat()
    assert "command" not in made["req"].kinds()
    assert "Remote start is not enabled" in log_text(domoticz)


def test_control_units_are_hidden_once_when_control_is_turned_off(run, domoticz):
    control_ready(run)
    actions = domoticz.Devices[HA].Units[planner.UNIT_ACTIONS]
    assert actions.stored["Used"] == 1
    plugin.onStop()
    run(AllowControl="false")
    assert actions.stored["Used"] == 0
    assert domoticz.Devices[HA].Units[planner.UNIT_PROGRAM_SELECT].stored["Used"] == 1
    assert saved_state(domoticz).control_shown is False


def test_a_program_selector_without_its_key_list_is_refused(run, domoticz):
    unit = domoticz.Unit(
        Name="Oven Program choice",
        DeviceID=HA,
        Unit=planner.UNIT_PROGRAM_SELECT,
        TypeName="Selector Switch",
        Switchtype=18,
        Options={"LevelNames": "-|Hete lucht"},
    )
    unit.Create()
    made = run(AllowControl="true")
    plugin.onCommand(HA, planner.UNIT_PROGRAM_SELECT, "Set Level", 10, "")
    assert "restore a backup" in log_text(domoticz)
    assert "command" not in made["req"].kinds()


def test_a_write_refused_with_401_is_sent_once_more_after_a_refresh(run):
    made = control_ready(run)
    plugin.onCommand(HA, planner.UNIT_PROGRAM_SELECT, "Set Level", 10, "")
    req = made["req"]
    write = req.take("command")
    fakes.push(req, fakes.result(req, write, 401, b"", "auth", "invalid_token"))
    plugin.onHeartbeat()
    fakes.push(req, fakes.result(req, req.take("refresh"), 200, token_body(ACCESS2)))
    plugin.onHeartbeat()
    again = req.take("command")
    assert again.retried and (again.command_id, again.body) == (write.command_id, write.body)


def test_a_write_the_worker_refuses_is_reported_with_the_reason(run, domoticz):
    made = control_ready(run)
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", START, "")
    req = made["req"]
    fakes.push(req, workers.Refused(req.generation, req.take("command"), "limiter", 20.0))
    plugin.onHeartbeat()
    assert "limit reached (5 per minute); try again in 20 s" in log_text(domoticz)


def test_a_stop_refused_for_no_active_program_says_nothing_runs(run, domoticz):
    made = control_ready(run)
    event(made, "STATUS", items((OPERATION, STATE + "Run")))
    plugin.onHeartbeat()
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", STOP, "")
    event(made, "STATUS", items((OPERATION, STATE + "Ready")))
    key = "SDK.Error.NoProgramActive"
    reply(made, "command", 404, b"", "state", key)
    assert "No program is running on Oven" in log_text(domoticz)


def test_a_refused_command_names_the_device(run, domoticz):
    control_ready(run)
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", 0, "")
    [line] = [m for _, m in domoticz._log if "command_refused" in m]
    assert 'device="Oven Actions"' in line and "unit=" not in line


def test_a_write_that_cannot_be_queued_is_refused(run, domoticz):
    made = control_ready(run)
    made["req"].accepting = False
    plugin.onCommand(HA, planner.UNIT_PROGRAM_SELECT, "Set Level", 10, "")
    assert "Too many requests queued" in log_text(domoticz)


def test_lost_authorization_cancels_queued_writes(run, domoticz):
    made = control_ready(run)
    plugin.onCommand(HA, setpoint_unit(), "Set Level", 200.0, "")
    plugin._lose_authorization(auth.AuthState.NEEDS_REAUTH, "invalid_grant")
    assert "command_cancelled" in log_text(domoticz)
    assert "command" not in made["req"].kinds()


def test_actions_return_to_their_neutral_level_and_the_same_action_runs_twice(run, domoticz):
    # Spec 6.2 unit 9: a scene that stores Start runs it every time, not only the first time.
    made = control_ready(run)
    actions = domoticz.Devices[HA].Units[planner.UNIT_ACTIONS]
    sent = []
    for _ in range(2):
        actions.nValue, actions.sValue = START, str(START)  # as left by the user's choice
        plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", START, "")
        assert (actions.nValue, actions.sValue) == (0, "0")
        refusal = "BSH.Common.Error.RemoteStartNotActive"
        sent.append(reply(made, "command", 409, b"", "refused_state", refusal))
    assert [job.path.endswith("/programs/active") for job in sent] == [True, True]
    assert sent[0].command_id != sent[1].command_id


def delayed_oven(made):
    """The oven waits for a delayed start of the selected program, an hour from now."""
    event(made, "STATUS", items((OPERATION, STATE + "DelayedStart")))
    event(made, "NOTIFY", items(("BSH.Common.Root.ActiveProgram", OVEN_PROGRAM), (START_IN, 3600)))
    plugin.onHeartbeat()
    made["req"].jobs.clear()


@pytest.mark.parametrize("status, held", [(204, 18 * 60), (409, 17 * 60)])
def test_a_delayed_start_move_is_held_only_once_confirmed(run, domoticz, status, held):
    made = control_ready(run)
    s = plugin._s()
    s.state.delayed_start[HA] = 17 * 60
    delayed_oven(made)
    level = planner.delay_level(18 * 60)
    plugin.onCommand(HA, planner.UNIT_DELAYED_START, "Set Level", level, "")
    assert "Moving the delayed start to 18:00" in log_text(domoticz)
    assert s.state.delayed_start[HA] == 17 * 60  # not before Home Connect has answered
    move = reply(made, "command", status, b"", "" if status < 400 else "refused_state")
    assert move.body["data"]["key"] == START_IN
    assert s.state.delayed_start[HA] == held
    unit = domoticz.Devices[HA].Units[planner.UNIT_DELAYED_START]
    assert unit.sValue == str(planner.delay_level(held))


def test_a_delayed_start_move_shows_at_once_and_reverts_on_refusal(run, domoticz):
    made = control_ready(run)
    plugin._s().state.delayed_start[HA] = 17 * 60
    delayed_oven(made)
    unit = domoticz.Devices[HA].Units[planner.UNIT_DELAYED_START]
    plugin.onCommand(HA, planner.UNIT_DELAYED_START, "Set Level", planner.delay_level(18 * 60), "")
    assert unit.sValue == str(planner.delay_level(18 * 60))
    reply(made, "command", 409, b"", "refused_state")
    assert unit.sValue == str(planner.delay_level(17 * 60))


def test_a_blocked_program_selector_stays_blocked_across_restarts(run, domoticz):
    options = {"LevelNames": "-|Old program"}
    unit = domoticz.Unit(
        Name="Oven Program choice",
        DeviceID=HA,
        Unit=planner.UNIT_PROGRAM_SELECT,
        TypeName="Selector Switch",
        Switchtype=18,
        Options=options,
    )
    unit.Create()
    control_ready(run)
    assert unit.stored["Options"] == options
    plugin.onStop()
    made = run(AllowControl="true")
    assert (HA, planner.UNIT_PROGRAM_SELECT) in plugin._s().blocked_selectors
    plugin.onCommand(HA, planner.UNIT_PROGRAM_SELECT, "Set Level", 10, "")
    assert "restore a backup" in log_text(domoticz)
    assert "command" not in made["req"].kinds()


def test_a_blocked_option_selector_stays_blocked_across_restarts(run, domoticz):
    # Spec 4.4: a fresh level map from today's values would give a stored scene level another
    # value, and writing its key list would unblock it at the next start.
    options = {"HCKey": MODE, "LevelNames": "-|Old mode"}
    unit = domoticz.Unit(
        Name="Oven Heat-up",
        DeviceID=HA,
        Unit=25,
        TypeName="Selector Switch",
        Switchtype=18,
        Options=options,
    )
    unit.Create()
    mode = {
        "key": MODE,
        "type": "Cooking.Oven.EnumType.HeatupMode",
        "constraints": {"allowedvalues": ["Cooking.Oven.EnumType.HeatupMode.Fast"]},
    }
    control_ready(run, extra_options=[mode])
    assert unit.stored["Options"] == options
    assert "25" not in plugin._s().state.level_maps.get(HA, {})
    plugin.onStop()
    made = run(AllowControl="true")
    assert (HA, 25) in plugin._s().blocked_selectors
    plugin.onCommand(HA, 25, "Set Level", 10, "")
    assert "restore a backup" in log_text(domoticz)
    assert "command" not in made["req"].kinds()


def test_a_write_answered_offline_marks_the_appliance_offline(run):
    made = control_ready(run)
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", START, "")
    reply(made, "command", 409, b"", errors.OFFLINE, "SDK.Error.HomeAppliance.Connection.Failed")
    assert not plugin._s().model.appliances[HA].connected


def test_choosing_no_delayed_start_clears_the_held_time(run, domoticz):
    control_ready(run)
    s = plugin._s()
    s.state.delayed_start[HA] = 17 * 60
    plugin.onCommand(HA, planner.UNIT_DELAYED_START, "Off", 0, "")
    assert HA not in s.state.delayed_start
    assert "Delayed start cleared" in log_text(domoticz)


def test_a_rounded_value_is_logged_with_the_command(run, domoticz):
    control_ready(run)
    plugin.onCommand(HA, setpoint_unit(), "Set Level", 203.0, "")
    assert "command_adjusted" in log_text(domoticz)
    assert "rounded to 205" in log_text(domoticz)


def test_a_check_that_cannot_be_queued_decides_on_what_is_known(run, domoticz):
    made = control_ready(run)
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", START, "")
    made["req"].accepting = False
    reply(made, "command", 0, category="network")
    assert "did not start" in log_text(domoticz)
    assert not plugin._s().coord.busy()


def test_a_delay_beyond_the_appliance_limit_is_clamped_when_start_is_sent(
    run, domoticz, monkeypatch
):
    made = control_ready(run, start_limits={"min": 0, "max": 3600, "stepsize": 60})
    now = time.time()
    monkeypatch.setattr(plugin, "_wall", lambda: now)
    local = time.localtime(now)
    minutes = ((local.tm_hour * 60 + local.tm_min) // 30 * 30 + 180) % (24 * 60)
    plugin.onCommand(HA, planner.UNIT_DELAYED_START, "Set Level", planner.delay_level(minutes), "")
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", START, "")
    start = made["req"].take("command")
    assert {"key": START_IN, "value": 3600} in start.body["data"]["options"]
    assert "later than the appliance allows" in log_text(domoticz)


def test_stopping_reports_queued_and_sent_writes_and_sends_nothing_more(run, domoticz):
    made = control_ready(run)
    plugin.onCommand(HA, planner.UNIT_PROGRAM_SELECT, "Set Level", 10, "")
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", START, "")
    made["req"].take("command")  # the selection is on its way; the Start waits behind it
    plugin.onStop()
    assert "command_cancelled" in log_text(domoticz)
    assert "command_unconfirmed" in log_text(domoticz)
    assert "Plugin stopping" in log_text(domoticz)
    assert made["req"].jobs == []
    assert not plugin._s().coord.busy()


def test_stopping_puts_the_observed_value_back_on_a_cancelled_write(run, domoticz):
    made = control_ready(run)
    unit = domoticz.Devices[HA].Units[setpoint_unit()]
    shown = unit.sValue
    plugin.onCommand(HA, setpoint_unit(), "Set Level", 200.0, "")
    assert unit.sValue != shown  # the request is shown while it is pending
    plugin.onStop()
    assert "command_cancelled" in log_text(domoticz)
    assert unit.sValue == shown
    assert made["req"].stopped and made["stream"].stopped


def test_stop_stops_the_workers_when_restoring_the_devices_fails(run, domoticz, monkeypatch):
    made = control_ready(run)
    plugin.onCommand(HA, setpoint_unit(), "Set Level", 200.0, "")

    def broken():
        raise RuntimeError("devices gone")

    monkeypatch.setattr(plugin, "_write_devices", broken)
    plugin.onStop()
    assert made["req"].stopped and made["stream"].stopped


def test_a_blocked_selector_is_refused_as_control_off_while_control_is_off(run, domoticz):
    domoticz.Unit(
        Name="Oven Program choice",
        DeviceID=HA,
        Unit=planner.UNIT_PROGRAM_SELECT,
        TypeName="Selector Switch",
        Switchtype=18,
        Options={"LevelNames": "-|Hete lucht"},
    ).Create()
    run()
    plugin.onCommand(HA, planner.UNIT_PROGRAM_SELECT, "Set Level", 10, "")
    refused = [m for _, m in domoticz._log if "command_refused" in m]
    assert len(refused) == 1 and "'Allow control' is off" in refused[0]


def test_a_start_for_an_unknown_appliance_fails_alone(run, domoticz):
    made = control_ready(run)
    s = plugin._s()
    other = "BOSCH-HCS99XXX1-000000000099"
    request = control.start_request(other, OVEN_PROGRAM, [])
    lost = control.Intent(other, "start", OVEN_PROGRAM, None, request, "Start", limit="start")
    s.coord.submit(lost, plugin._mono())
    plugin.onCommand(HA, planner.UNIT_PROGRAM_SELECT, "Set Level", 10, "")
    assert made["req"].take("command").ha_id == HA
    assert "not discovered yet" in log_text(domoticz)


def test_a_check_hit_by_a_401_after_authorization_was_lost_still_settles(run, domoticz, clock):
    made = control_ready(run)
    unit = domoticz.Devices[HA].Units[setpoint_unit()]
    plugin.onCommand(HA, setpoint_unit(), "Set Level", 200.0, "")
    clock.offset += coordinator.SETPOINT_DEBOUNCE_S
    plugin.onHeartbeat()
    reply(made, "command")
    clock.offset += coordinator.CONFIRM_S
    plugin.onHeartbeat()
    req = made["req"]
    check = req.take("verify")
    plugin._lose_authorization(auth.AuthState.NEEDS_REAUTH, "invalid_grant")
    fakes.push(req, fakes.result(req, check, 401, b"", "auth", "invalid_token"))
    plugin.onHeartbeat()
    assert "command_unconfirmed" in log_text(domoticz)
    assert not plugin._s().coord.busy() and plugin._s().coord.overlay() == {}
    assert unit.sValue == "180.0"
    assert domoticz._heartbeat == plugin.HEARTBEAT_S


def test_a_write_held_for_a_refresh_that_fails_is_reported_not_sent(run, domoticz):
    made = control_ready(run)
    plugin.onCommand(HA, planner.UNIT_PROGRAM_SELECT, "Set Level", 10, "")
    req = made["req"]
    fakes.push(req, fakes.result(req, req.take("command"), 401, b"", "auth", "invalid_token"))
    plugin.onHeartbeat()
    refresh = req.take("refresh")
    fakes.push(req, fakes.result(req, refresh, 400, oauth_error("invalid_grant"), "oauth"))
    plugin.onHeartbeat()
    assert plugin._s().auth_state == "NEEDS_REAUTH"
    cancelled = [m for _, m in domoticz._log if "command_cancelled" in m]
    assert len(cancelled) == 1 and "Not signed in" in cancelled[0]
    assert "command_unconfirmed" not in log_text(domoticz)
    assert not plugin._s().coord.busy()


def test_a_write_refused_401_again_after_the_refresh_is_reported_not_sent(run, domoticz):
    made = control_ready(run)
    plugin.onCommand(HA, planner.UNIT_PROGRAM_SELECT, "Set Level", 10, "")
    req = made["req"]
    for _ in range(2):
        fakes.push(req, fakes.result(req, req.take("command"), 401, b"", "auth", "invalid_token"))
        plugin.onHeartbeat()
        if plugin._s().refresh_in_flight:
            fakes.push(req, fakes.result(req, req.take("refresh"), 200, token_body(ACCESS2)))
            plugin.onHeartbeat()
    assert plugin._s().auth_state == "NEEDS_REAUTH"
    assert "command_cancelled" in log_text(domoticz)
    assert "command_unconfirmed" not in log_text(domoticz)


def test_a_value_already_shown_sends_nothing(run, domoticz, clock):
    made = control_ready(run)
    plugin.onCommand(HA, setpoint_unit(), "Set Level", 180.0, "")
    clock.offset += coordinator.SETPOINT_DEBOUNCE_S
    plugin.onHeartbeat()
    assert "command" not in made["req"].kinds()
    skipped = [m for _, m in domoticz._log if "command_skipped" in m]
    assert len(skipped) == 1 and "Set temperature: 180" in skipped[0]
    assert not plugin._s().coord.busy()


def test_stop_stops_the_workers_and_saves_when_reporting_pending_writes_fails(
    run, domoticz, monkeypatch
):
    made = control_ready(run)
    plugin.onCommand(HA, planner.UNIT_ACTIONS, "Set Level", START, "")

    def broken(notes):
        raise RuntimeError("log sink gone")

    monkeypatch.setattr(plugin, "_notes", broken)
    plugin.onStop()
    assert made["req"].stopped and made["stream"].stopped
    assert saved_state(domoticz).control_shown is True
    assert "stop failed (RuntimeError)" in log_text(domoticz)


def test_a_move_confirmed_after_the_program_started_holds_nothing(run, domoticz):
    made = control_ready(run)
    s = plugin._s()
    s.state.delayed_start[HA] = 17 * 60
    delayed_oven(made)
    plugin.onCommand(HA, planner.UNIT_DELAYED_START, "Set Level", planner.delay_level(18 * 60), "")
    event(made, "STATUS", items((OPERATION, STATE + "Run")))
    plugin.onHeartbeat()
    assert HA not in s.state.delayed_start  # spent by the start
    reply(made, "command")
    assert HA not in s.state.delayed_start


def held_for_refresh(made):
    """A selection answered 401: it waits for the refreshed token."""
    plugin.onCommand(HA, planner.UNIT_PROGRAM_SELECT, "Set Level", 10, "")
    req = made["req"]
    fakes.push(req, fakes.result(req, req.take("command"), 401, b"", "auth", "invalid_token"))
    plugin.onHeartbeat()


def failed_lines(domoticz):
    return [m for _, m in domoticz._log if "command_failed" in m]


def test_a_write_held_for_a_refresh_is_reported_not_sent_when_a_new_grant_replaces_it(
    run, domoticz
):
    made = control_ready(run)
    held_for_refresh(made)
    plugin._grant(live_tokens())
    [line] = failed_lines(domoticz)
    assert "give the command again" in line
    assert not plugin._s().coord.busy()
    assert "command" not in made["req"].kinds()


def test_a_write_that_finds_the_refresh_queue_full_is_reported_not_sent(run, domoticz):
    made = control_ready(run)
    s = plugin._s()
    s.after_refresh.extend([s.sched.list_job(s.model)] * plugin.AWAITING_TOKEN_MAX)
    held_for_refresh(made)
    [line] = failed_lines(domoticz)
    assert "try again in a minute" in line
    assert not plugin._s().coord.busy()


def test_a_write_turned_away_without_a_token_and_no_room_to_wait_is_reported(run, domoticz):
    made = control_ready(run)
    s = plugin._s()
    s.refresh_wanted = True
    s.after_refresh.extend([s.sched.list_job(s.model)] * plugin.AWAITING_TOKEN_MAX)
    plugin.onCommand(HA, planner.UNIT_PROGRAM_SELECT, "Set Level", 10, "")
    req = made["req"]
    fakes.push(req, workers.Refused(req.generation, req.take("command"), "no_token"))
    plugin.onHeartbeat()
    [line] = failed_lines(domoticz)
    assert "try again in a minute" in line


def test_a_held_write_that_cannot_be_queued_after_the_refresh_is_reported(run, domoticz):
    made = control_ready(run)
    held_for_refresh(made)
    req = made["req"]
    refresh = req.take("refresh")
    req.accepting = False
    fakes.push(req, fakes.result(req, refresh, 200, token_body(ACCESS2)))
    plugin.onHeartbeat()
    [line] = failed_lines(domoticz)
    assert "Too many requests queued" in line
    assert not plugin._s().coord.busy()


# -- plain log wording ----------------------------------------------------------------------------


def status_lines(domoticz):
    return [m for level, m in domoticz._log if level == "status"]


def test_sign_in_status_lines_read_as_plain_words(run, domoticz):
    made = run()
    start_flow(made)
    assert 'started account="not signed in yet"' in status_lines(domoticz)
    [line] = [m for m in status_lines(domoticz) if m.startswith("sign_in_needed ")]
    assert "ABCD-EFGH" in line and "link=" in line
    approve(made)
    assert "signed_in" in status_lines(domoticz)
    plugin.onStop()
    run()
    assert 'started account="signed in"' in status_lines(domoticz)
    # No status line carries a raw state name such as "AUTHORIZED" for the user to decode.
    for line in status_lines(domoticz):
        assert "AUTHORIZED" not in line and "AWAITING_USER" not in line, line


def test_an_expired_sign_in_code_says_what_to_do(run, domoticz, monkeypatch):
    made = run()
    start_flow(made)
    monkeypatch.setattr(plugin, "_wall", lambda: time.time() + 4000)
    plugin.onHeartbeat()
    [line] = [m for m in status_lines(domoticz) if m.startswith("sign_in_code_expired")]
    assert "Sign in again" in line


def test_existing_account_selector_gets_the_new_level_names(run, domoticz):
    old = planner._selector_options(["-", "Re-authorize", "Refresh appliances"], SelectorStyle="0")
    unit = domoticz.Unit(
        Name="Home Connect actions",
        DeviceID=planner.ACCOUNT_DEVICE,
        Unit=planner.ACCOUNT_ACTIONS,
        TypeName="Selector Switch",
        Options=old,
        Used=1,
    )
    unit.Create()
    run()
    plugin.onHeartbeat()
    levels = unit.stored["Options"]["LevelNames"]
    assert levels == "-|Sign in again|Refresh appliances"
    assert plugin.REAUTHORIZE_LEVEL == 10 and plugin.REFRESH_LEVEL == 20  # numbers never move


def test_refresh_while_signed_out_names_what_was_refused(run, domoticz):
    run()
    plugin._account_action(plugin.REFRESH_LEVEL)
    assert 'refresh_appliances_refused detail="not signed in"' in status_lines(domoticz)


def test_recreated_account_units_start_hidden_while_signed_in(run, domoticz):
    made = run()
    authorize(made)
    del domoticz.Devices[planner.ACCOUNT_DEVICE]  # the user deleted the account devices
    plugin.onHeartbeat()
    assert account_unit(domoticz).stored["Used"] == 0
    assert account_unit(domoticz, planner.ACCOUNT_ACTIONS).stored["Used"] == 0
