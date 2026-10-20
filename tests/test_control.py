import re
import time

import pytest

import budget
import catalog
import control
import errors
import model
import planner
import scheduler

HA = "SIEMENS-HCS01OVN1-000000000001"
SETPOINT = "Cooking.Oven.Option.SetpointTemperature"
PROGRAM = "Cooking.Oven.Program.HeatingMode.HotAir"
STATE = "BSH.Common.EnumType.OperationState."


def local(hour, minute=0, day=15):
    """A wall time at a local clock time in January (no DST change), so every zone agrees."""
    return time.mktime((2027, 1, day, hour, minute, 0, 0, 0, -1))


def oven(**status):
    app = model.Appliance(HA, name="Oven", type="Oven", connected=True)
    for key, value in status.items():
        app.status[key] = model.Value(value)
    return app


def observe(app, store, key, value, seq):
    store[key] = model.Value(value)
    app.seq[key] = seq


def intent(action, key="", value=None):
    return control.Intent(HA, action, key, value, None, action)


def test_every_message_exists_in_both_languages_with_the_same_fields():
    for name, (english, dutch) in control._TEXT.items():
        assert english and dutch, name
        fields = set(re.findall(r"{(\w+)}", english))
        assert fields == set(re.findall(r"{(\w+)}", dutch)), name
    assert (
        control.message("offline", "nl", a="Oven")
        == "Oven is offline; controleer de stroom en wifi van het apparaat"
    )


def test_requests_have_the_shapes_home_connect_documents():
    base = f"/api/homeappliances/{HA}"
    select = control.select_request(HA, PROGRAM)
    assert select == control.Request("PUT", f"{base}/programs/selected", {"data": {"key": PROGRAM}})
    option = control.option_request(HA, SETPOINT, 200, "°C")
    assert option.path == f"{base}/programs/selected/options/{SETPOINT}"
    assert option.body == {"data": {"key": SETPOINT, "value": 200, "unit": "°C"}}
    running = control.option_request(HA, SETPOINT, 200, running=True)
    assert running.path.endswith(f"/programs/active/options/{SETPOINT}")
    assert running.body == {"data": {"key": SETPOINT, "value": 200}}
    lock = "BSH.Common.Setting.ChildLock"
    assert control.setting_request(HA, lock, True).body == {"data": {"key": lock, "value": True}}
    assert control.start_request(HA, PROGRAM, []).body == {"data": {"key": PROGRAM}}
    start = control.start_request(HA, PROGRAM, [{"key": SETPOINT, "value": 180}])
    assert (start.method, start.path) == ("PUT", f"{base}/programs/active")
    assert start.body == {"data": {"key": PROGRAM, "options": [{"key": SETPOINT, "value": 180}]}}
    assert control.stop_request(HA) == control.Request("DELETE", f"{base}/programs/active")
    pause = control.command_request(HA, catalog.PAUSE)
    assert (pause.path, pause.body) == (
        f"{base}/commands/{catalog.PAUSE}",
        {"data": {"key": catalog.PAUSE, "value": True}},
    )
    with pytest.raises(ValueError):
        control.option_request(HA, "../x", 1)


def test_numbers_are_checked_against_the_range_and_snapped_to_the_step():
    rng = control.value_range({"min": 30, "max": 249, "stepsize": 5}, "Double")
    assert control.in_range(30, rng) and control.in_range(249, rng)
    assert not control.in_range(29.9, rng) and not control.in_range(280, rng)
    assert control.snap(182, rng) == 180 and control.snap(183, rng) == 185
    assert control.snap(249, rng) == 245  # the step grid never goes past max
    whole = control.value_range({"min": 0, "max": 10}, "Int")
    assert whole.step == 1 and control.snap(2.4, whole) == 2
    free = control.value_range({}, "Double")
    assert free == control.Range(None, None, None) and control.snap(2.25, free) == 2.25
    assert control.value_range({"stepsize": 0, "min": True}, "") == control.Range(None, None, None)
    assert control.wire_number(180.0, "Double") == 180
    assert isinstance(control.wire_number(180.0, "Double"), int)
    assert control.wire_number(2.5, "Double") == 2.5 and control.wire_number(3.0, "Int") == 3


def test_command_levels_are_read_as_finite_numbers():
    assert control.as_number(20) == 20.0 and control.as_number("17.5") == 17.5
    for bad in (None, True, "x", float("nan"), "1e400", [1]):
        assert control.as_number(bad) is None


def test_range_text_names_the_bounds_that_exist():
    assert control.range_text(control.Range(30, 250, 5), "°C", "en") == "30 °C to 250 °C"
    assert control.range_text(control.Range(30, None, None), "", "nl") == "minimaal 30"
    assert control.range_text(control.Range(None, 9.5, None), "", "en") == "at most 9.5"


def test_next_time_is_today_when_still_ahead_and_tomorrow_otherwise():
    now = local(8)
    assert control.next_time(17 * 60, now) == local(17)
    assert control.next_time(7 * 60, now) == local(7, day=16)
    assert control.next_time(8 * 60, now) == local(8, day=16)  # within the lead: tomorrow


def test_a_time_after_midnight_counts_across_it():
    assert control.next_time(30, local(23, 45)) - local(23, 45) == 45 * 60


def test_delay_seconds_round_down_to_the_step():
    now = local(8) + 20
    delay = control.delay_seconds(17 * 60, {"min": 0, "max": 86400, "stepsize": 60}, now, "en")
    assert delay == control.Delay(9 * 3600 - 60)


def test_delay_beyond_the_maximum_is_clamped_with_a_note():
    delay = control.delay_seconds(17 * 60, {"max": 3600}, local(8), "en")
    assert isinstance(delay, control.Delay) and delay.seconds == 3600
    assert "17:00" in delay.note and "09:00" in delay.note


def test_delay_below_the_minimum_is_refused_with_the_earliest_time():
    refusal = control.delay_seconds(9 * 60, {"min": 7200}, local(8), "en")
    assert isinstance(refusal, control.Refusal)
    assert "09:00" in refusal.reason and "10:00" in refusal.reason


def test_start_carries_only_observed_writable_options_of_the_program():
    app = oven()
    fast = "Cooking.Oven.Option.FastPreHeat"
    app.cache.program_details[PROGRAM] = {
        "key": PROGRAM,
        "options": [
            {"key": SETPOINT, "type": "Double"},
            {"key": "BSH.Common.Option.Duration", "type": "Int"},
            {"key": fast, "constraints": {"access": "read"}},
            {"key": catalog.START_IN, "type": "Int"},
        ],
    }
    app.options[SETPOINT] = model.Value(180)
    app.options[fast] = model.Value(True)
    app.options[catalog.REMAINING] = model.Value(600)
    app.options[catalog.START_IN] = model.Value(3600)
    assert control.start_options(app, PROGRAM) == [{"key": SETPOINT, "value": 180}]
    assert control.delay_option_of(app, PROGRAM) == catalog.START_IN
    assert control.delay_option_of(app, "Other.Program") is None
    assert control.program_option(app, PROGRAM, fast) == {
        "key": fast,
        "constraints": {"access": "read"},
    }
    assert not control.writable(control.program_option(app, PROGRAM, fast))
    assert control.writable(control.program_option(app, PROGRAM, SETPOINT))
    assert not control.writable(None)


def test_a_write_is_confirmed_only_by_a_newer_matching_observation():
    app = oven()
    write = intent("option", SETPOINT, 200)
    observe(app, app.options, SETPOINT, 200, seq=5)
    assert not control.confirmed(write, app, since=5)
    observe(app, app.options, SETPOINT, 200.0, seq=6)
    assert control.confirmed(write, app, since=5)
    observe(app, app.options, SETPOINT, 180, seq=7)
    assert not control.confirmed(write, app, since=5)
    assert not control.confirmed(write, None, since=0)


def test_select_start_stop_and_commands_confirm_from_the_model():
    app = oven()
    app.selected_program = PROGRAM
    app.seq[model.SELECTED_ROOT] = 3
    assert control.confirmed(intent("select", PROGRAM), app, since=2)
    observe(app, app.status, model.OPERATION_STATE, STATE + "DelayedStart", seq=9)
    assert control.confirmed(intent("start"), app, since=8)
    assert not control.confirmed(intent("stop"), app, since=8)
    observe(app, app.status, model.OPERATION_STATE, STATE + "Pause", seq=10)
    assert control.confirmed(intent("command", catalog.PAUSE, True), app, since=8)
    assert not control.confirmed(intent("command", catalog.RESUME, True), app, since=8)
    observe(app, app.status, model.OPERATION_STATE, STATE + "Ready", seq=11)
    assert control.confirmed(intent("stop"), app, since=8)
    assert not control.confirmed(intent("command", catalog.ACKNOWLEDGE, True), app, since=8)
    observe(app, app.settings, "K.Setting.X", True, seq=12)
    assert control.confirmed(intent("setting", "K.Setting.X", True), app, since=11)
    assert not control.confirmed(intent("delay", catalog.START_IN, 60), app, since=0)


def test_failures_are_explained_with_the_fix_or_home_connects_words():
    remote = "BSH.Common.Error.RemoteStartNotActive"
    assert control.failure_reason(
        errors.REFUSED_USER, remote, "x", "Washer", "en"
    ) == errors.user_message(remote, "Washer", "en")
    validation = control.failure_reason(
        errors.REFUSED_VALIDATION, "SDK.Error.InvalidOptionValue", "Value 300 out of range\n",
        "Oven", "en",
    )  # fmt: skip
    assert validation == "Value 300 out of range"
    offline = control.failure_reason(errors.OFFLINE, "", "", "Oven", "nl")
    assert offline == "Oven is offline; controleer de stroom en wifi van het apparaat"
    wrong = "SDK.Error.WrongOperationState"
    assert wrong in control.failure_reason(errors.STATE, wrong, "", "Oven", "en")
    limited = control.failure_reason(errors.LIMITED, "", "", "Oven", "en")
    assert limited == control.message("blocked_now", "en")
    forbidden = control.failure_reason(errors.FORBIDDEN, "insufficient_scope", "", "Oven", "en")
    assert forbidden == control.message("forbidden", "en")
    limiter = control.message("limiter_now", "en", limit=budget.ACTION_LIMIT)
    assert control.refused_reason("limiter", "en") == limiter
    assert control.refused_reason("queue_full", "en").endswith("; try again in a minute")
    assert control.refused_reason("stale", "en").endswith("; give the command again")
    assert "odd" in control.refused_reason("odd", "en")


def test_a_held_delay_is_spent_once_the_appliance_leaves_delayed_start():
    app = oven(**{model.OPERATION_STATE: STATE + "DelayedStart"})
    previous, held = {}, {HA: 17 * 60}
    assert control.spent_delays(previous, [app], held) == []
    app.status[model.OPERATION_STATE] = model.Value(None)  # unknown for a moment: no change
    assert control.spent_delays(previous, [app], held) == [] and held
    app.status[model.OPERATION_STATE] = model.Value(STATE + "Run")
    assert control.spent_delays(previous, [app], held) == [HA] and held == {}


def test_write_and_verify_jobs_are_commands_with_a_deadline():
    start = control.Intent(
        HA, "start", PROGRAM, None, control.start_request(HA, PROGRAM, []), "Start",
        limit="start", verify="status",
    )  # fmt: skip
    job = control.write_job(7, start, issued=11, deadline=130.0)
    assert (job.kind, job.method, job.budget_class, job.priority) == (
        "command",
        "PUT",
        budget.COMMAND,
        scheduler.PRIORITY_COMMAND,
    )
    assert (job.command_id, job.issued_seq, job.ttl_deadline, job.limit) == (7, 11, 130.0, "start")
    assert job.body == {"data": {"key": PROGRAM}}
    check = control.verify_job(7, start, issued=12, deadline=140.0)
    assert (check.kind, check.method, check.key, check.command_id) == ("verify", "GET", "status", 7)
    assert check.path == f"/api/homeappliances/{HA}/status"
    with pytest.raises(ValueError):
        control.write_job(8, control.Intent(HA, "stop", "", None, None, "Stop"), 1, 1.0)


def test_delay_option_prefers_finish_over_start_and_ignores_other_keys():
    assert planner.pick_delay_key([catalog.START_IN, catalog.FINISH_IN]) == catalog.FINISH_IN
    assert planner.pick_delay_key(["x", None]) is None


def test_unknown_failures_and_refusals_fall_back_to_the_key():
    assert "SDK.Odd" in control.failure_reason(errors.SERVER, "SDK.Odd", "", "Oven", "en")
    assert control.failure_reason(errors.UNKNOWN, "", "", "Oven", "en").endswith("(unknown)")
    assert control.refused_reason("brake", "nl") == control.message("brake_now", "nl")


def test_delay_step_and_missing_ranges_pass_through():
    now = local(8)
    plain = control.delay_seconds(17 * 60, {}, now, "en")
    assert plain == control.Delay(9 * 3600)
    stepped = control.delay_seconds(17 * 60, {"stepsize": 300, "min": 0}, now + 100, "en")
    assert isinstance(stepped, control.Delay) and stepped.seconds % 300 == 0


def test_the_earliest_time_named_in_a_refusal_is_one_the_user_can_pick():
    wall = local(8) + 20
    refusal = control.delay_seconds(10 * 60, {"min": 7200}, wall, "en")
    assert isinstance(refusal, control.Refusal)
    assert "the earliest is 10:30" in refusal.reason
    # Choosing the named slot is accepted.
    accepted = control.delay_seconds(10 * 60 + 30, {"min": 7200}, wall, "en")
    assert isinstance(accepted, control.Delay)


def test_implausibly_large_bounds_and_values_are_not_numbers():
    huge = 10**15
    assert control.value_range({"min": huge, "max": huge, "stepsize": huge}, "Double") == (
        control.Range(None, None, None)
    )
    assert control.as_number(huge) is None and control.as_number(str(huge)) is None


WRONG_STATE = "SDK.Error.WrongOperationState"


def state_reason(action, key, error_key, app, lang="en", describe="Pause"):
    write = control.Intent(HA, action, key, None, None, describe)
    return control.answer_reason(write, error_key, app, "Oven", lang)


def test_a_state_refusal_of_a_write_names_the_fix_decide_would_name():
    def at(state):
        return oven(**{model.OPERATION_STATE: STATE + state})

    nothing = control.message("nothing_to_stop", "en", a="Oven")
    assert state_reason("stop", "", "SDK.Error.NoProgramActive", at("Ready")) == nothing
    finished = catalog.state_name("Finished", "en")
    assert state_reason("start", PROGRAM, WRONG_STATE, at("Finished")) == control.message(
        "not_ready_finished", "en", a="Oven", state=finished
    )
    assert state_reason("command", catalog.PAUSE, WRONG_STATE, at("Ready")) == (
        "Pause is not possible on Oven now (Ready); it works only while Running"
    )
    assert state_reason("select", PROGRAM, WRONG_STATE, at("Run")) == control.message(
        "select_running", "en", a="Oven"
    )
    name = planner.program_label(at("Ready"), PROGRAM, "nl")
    assert state_reason("start", PROGRAM, "SDK.Error.ProgramNotAvailable", at("Ready"), "nl") == (
        control.message("program_not_offered", "nl", program=name, a="Oven")
    )
    assert state_reason("option", "K", "SDK.Error.SelectedProgramNotSet", at("Ready")) == (
        control.message("no_program_selected", "en", a="Oven")
    )


def test_a_state_refusal_the_model_cannot_explain_keeps_home_connects_key():
    # Seen Ready, a refused Start has no better fix to name than the key; nor has an unknown
    # state, a key with no matching fix, or a write the mapping does not cover.
    assert (
        state_reason(
            "start", PROGRAM, WRONG_STATE, oven(**{model.OPERATION_STATE: STATE + "Ready"})
        )
        is None
    )
    assert state_reason("start", PROGRAM, WRONG_STATE, oven()) is None
    assert state_reason("start", PROGRAM, WRONG_STATE, None) is None
    assert state_reason("select", PROGRAM, WRONG_STATE, oven()) is None
    assert (
        state_reason("option", "K", WRONG_STATE, oven(**{model.OPERATION_STATE: STATE + "Run"}))
        is None
    )
    assert state_reason("option", "K", "SDK.Error.ProgramNotAvailable", oven()) is None
    assert state_reason("setting", "K", "SDK.Error.NoProgramActive", oven()) is None


@pytest.mark.parametrize(
    ("reason", "wait_s", "expected"),
    [
        ("pace", 12.2, "Too many requests this minute; try again in 13 s"),
        ("limiter", 40.0, "Start or stop limit reached (5 per minute); try again in 40 s"),
        ("brake", 290.0, "Paused after repeated errors; try again in 5 min"),
        ("blocked", 30.0, "Home Connect asked to wait; try again in 30 s"),
        ("pace", 0.0, "Too many requests this minute"),
        ("total", 99.0, "Daily API budget used up; try again later"),
    ],
)
def test_a_worker_refusal_names_the_wait_it_knows(reason, wait_s, expected):
    assert control.refused_reason(reason, "en", wait_s) == expected


def test_the_control_off_message_quotes_the_setting_label_in_both_languages():
    label = "'Allow control'"
    assert control.message("control_off", "en").startswith(label)
    assert control.message("control_off", "nl").startswith(label)


def test_a_start_refused_while_a_program_waits_says_to_stop_it():
    app = oven(**{model.OPERATION_STATE: STATE + "DelayedStart"})
    app.selected_program = PROGRAM
    refusal = control.build_start(app, None, 0.0, "en", "Oven")
    assert isinstance(refusal, control.Refusal)
    assert refusal.reason.endswith("; stop the program waiting for its delayed start first")
