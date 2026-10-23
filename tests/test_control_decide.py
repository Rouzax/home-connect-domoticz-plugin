import time

import pytest

import budget
import catalog
import control
import errors
import model
import persistence
import planner

HA = "SIEMENS-HCS01OVN1-000000000001"
PROGRAM = "Cooking.Oven.Program.HeatingMode.HotAir"
OTHER = "Cooking.Oven.Program.HeatingMode.TopBottomHeating"
GONE = "Cooking.Oven.Program.HeatingMode.Gone"
SETPOINT = "Cooking.Oven.Option.SetpointTemperature"
SPIN = "LaundryCare.Washer.Option.SpinSpeed"
RPM = "LaundryCare.Washer.EnumType.SpinSpeed.RPM"
CHILD_LOCK = "BSH.Common.Setting.ChildLock"
STATE = "BSH.Common.EnumType.OperationState."
ON = "BSH.Common.EnumType.PowerState.On"
STANDBY = "BSH.Common.EnumType.PowerState.Standby"
SETPOINT_UNIT, LOCK_UNIT, DOOR_UNIT, SPIN_UNIT = 20, 21, 22, 23
START = planner.ACTION_LEVELS["start"]
STOP = planner.ACTION_LEVELS["stop"]
PAUSE = planner.ACTION_LEVELS["pause"]
RESUME = planner.ACTION_LEVELS["resume"]
EIGHT = time.mktime((2027, 1, 15, 8, 0, 0, 0, 0, -1))
_DEFAULT = object()


def snapshot(**kw):
    base = dict(used={}, total_used=0, blocked_for_s=0.0, braked=False, exhausted=frozenset())
    base.update(kw)
    return budget.BudgetSnapshot(**base)


def oven(state="Ready"):
    app = model.Appliance(HA, name="Oven", type="Oven", connected=True)
    app.status[model.OPERATION_STATE] = model.Value(STATE + state)
    app.cache.programs = [PROGRAM, OTHER]
    setpoint = {
        "key": SETPOINT,
        "type": "Double",
        "unit": "°C",
        "constraints": {"min": 30, "max": 250, "stepsize": 5, "liveupdate": True},
    }
    start_in = {
        "key": catalog.START_IN,
        "type": "Int",
        "unit": "seconds",
        "constraints": {"min": 0, "max": 86340, "stepsize": 60},
    }
    app.cache.program_details = {
        PROGRAM: {"key": PROGRAM, "options": [setpoint, start_in]},
        OTHER: {"key": OTHER, "options": []},
    }
    app.selected_program = PROGRAM
    app.options[SETPOINT] = model.Value(180, "°C")
    app.cache.commands = [catalog.ACKNOWLEDGE]
    app.cache.setting_constraints = {
        CHILD_LOCK: {"access": "readWrite"},
        catalog.POWER_STATE: {"access": "readWrite", "allowedvalues": [ON, STANDBY]},
    }
    return app


def door_oven():
    app = oven()
    app.cache.commands.append(catalog.OPEN_DOOR)
    return app


def make_state():
    return persistence.State(
        unit_alloc={
            HA: {SETPOINT: SETPOINT_UNIT, CHILD_LOCK: LOCK_UNIT, catalog.DOOR_STATE: DOOR_UNIT}
        },
        unit_kinds={HA: {str(SETPOINT_UNIT): "setpoint", str(LOCK_UNIT): "switch"}},
        level_maps={HA: {str(planner.UNIT_PROGRAM_SELECT): [PROGRAM, OTHER, GONE]}},
    )


def decide(unit, command="Set Level", level=0, app=_DEFAULT, state=None, **kw):
    base = dict(
        app=oven() if app is _DEFAULT else app,
        state=state or make_state(),
        label="Oven",
        lang="en",
        allow_control=True,
        authorized=True,
        snapshot=snapshot(),
        mono=1000.0,
        wall=EIGHT,
    )
    base.update(kw)
    return control.decide(HA, unit, command, level, control.Context(**base))


# -- gating --------------------------------------------------------------------------------------


def test_allow_control_off_refuses_every_unit_before_anything_else():
    refusal = control.Refusal(control.message("control_off", "en"))
    for unit in (1, 2, 8, 9, 10, 11, 12, SETPOINT_UNIT, 99):
        assert decide(unit, level=10, allow_control=False, app=None) == refusal


def test_read_only_units_and_keys_are_refused():
    refusal = control.Refusal(control.message("read_only", "en"))
    for unit in (planner.UNIT_STATE, planner.UNIT_DOOR, planner.UNIT_STATUS_CODE, DOOR_UNIT, 99):
        assert decide(unit, level=10) == refusal


def test_unknown_unauthorized_and_offline_appliances_are_refused():
    assert decide(9, level=START, app=None).reason == control.message("unknown_appliance", "en")
    assert decide(9, level=START, authorized=False).reason == control.message(
        "not_authorized", "en"
    )
    app = oven()
    app.connected = False
    assert (
        decide(9, level=START, app=app).reason
        == "Oven is offline; check the power and Wi-Fi of the appliance"
    )


# -- start ---------------------------------------------------------------------------------------


def test_start_sends_the_selected_program_with_its_observed_options():
    result = decide(planner.UNIT_ACTIONS, level=START)
    assert isinstance(result, control.Intent)
    assert (result.action, result.limit, result.verify, result.coalesce) == (
        "start",
        "start",
        "status",
        "",
    )
    assert result.request == control.start_request(HA, PROGRAM, [{"key": SETPOINT, "value": 180}])


def test_scene_start_with_remote_start_off_sends_nothing():
    app = oven()
    app.status[catalog.REMOTE_START] = model.Value(False)
    result = decide(planner.UNIT_ACTIONS, level=START, app=app)
    fix = errors.user_message("BSH.Common.Error.RemoteStartNotActive", "Oven", "en")
    assert result == control.Refusal(fix)


@pytest.mark.parametrize(
    ("key", "value", "error_key"),
    [
        (catalog.REMOTE_ACTIVE, False, "BSH.Common.Error.RemoteControlNotActive"),
        (catalog.LOCAL_CONTROL, True, "BSH.Common.Error.LockedByLocalControl"),
    ],
)
def test_each_start_precondition_names_its_fix(key, value, error_key):
    app = oven()
    app.status[key] = model.Value(value)
    reason = decide(planner.UNIT_ACTIONS, level=START, app=app).reason
    assert reason == errors.user_message(error_key, "Oven", "en")


def test_start_needs_ready_and_a_program():
    assert decide(9, level=START, app=oven("Run")).reason == (
        "Oven is not ready to start (Running); wait until it is Ready"
    )
    assert decide(9, level=START, app=oven("Inactive")).reason == (
        "Oven is not ready to start (Inactive); switch it on first"
    )
    assert decide(9, level=START, app=oven("Finished")).reason == (
        "Oven is not ready to start (Finished); open the door or acknowledge the end first"
    )
    app = oven()
    app.selected_program = None
    assert decide(9, level=START, app=app).reason == (
        "No program is selected on Oven; choose one first"
    )


def test_unreported_preconditions_count_as_met():
    app = oven()
    app.status.pop(model.OPERATION_STATE)
    assert isinstance(decide(planner.UNIT_ACTIONS, level=START, app=app), control.Intent)


def test_start_with_a_held_delay_appends_the_start_option():
    state = make_state()
    state.delayed_start[HA] = 17 * 60
    result = decide(planner.UNIT_ACTIONS, level=START, state=state)
    assert {"key": catalog.START_IN, "value": 9 * 3600} in result.request.body["data"]["options"]


def test_a_held_delay_the_program_cannot_take_refuses_the_start():
    state = make_state()
    state.delayed_start[HA] = 17 * 60
    app = oven()
    app.selected_program = OTHER
    result = decide(planner.UNIT_ACTIONS, level=START, app=app, state=state)
    assert result.reason == control.message("no_delay_option", "en")


def test_start_uses_a_pending_selection():
    result = decide(planner.UNIT_ACTIONS, level=START, pending_program=OTHER)
    assert result.request == control.start_request(HA, OTHER, [])


@pytest.mark.parametrize(
    ("snap", "expected"),
    [
        (snapshot(block_until_mono=1030.0), "try again in 30 s"),
        (snapshot(braked=True, brake_until_mono=1300.0), "try again in 5 min"),
        (snapshot(total_used=budget.TOTAL), "budget used up"),
        (snapshot(total_used=880, command_free_at=EIGHT + 3600), "try again after 09:00"),
        (snapshot(network_failed_mono=990.0), "not reachable"),
        (snapshot(action_free_at={"start": 1012.0}), "try again in 12 s"),
    ],
)
def test_budget_gates_refuse_a_start_early(snap, expected):
    assert expected in decide(planner.UNIT_ACTIONS, level=START, snapshot=snap).reason


@pytest.mark.parametrize(
    "snap",
    [
        snapshot(braked=True, brake_until_mono=900.0),  # the worker may send it as the probe
        snapshot(network_failed_mono=900.0),
        snapshot(action_free_at={"stop": 1012.0}),
    ],
)
def test_gates_that_have_passed_let_a_start_through(snap):
    assert isinstance(decide(planner.UNIT_ACTIONS, level=START, snapshot=snap), control.Intent)


# -- stop and commands ---------------------------------------------------------------------------


def test_stop_needs_a_running_program_unless_it_cancels_a_waiting_start():
    assert decide(9, level=STOP).reason == "No program is running on Oven; there is nothing to stop"
    cancel = decide(9, level=STOP, pending_start=True)
    assert (cancel.action, cancel.request) == ("stop", None)
    running = decide(9, level=STOP, app=oven("Run"))
    assert (running.request, running.limit, running.verify) == (
        control.stop_request(HA),
        "stop",
        "status",
    )


def test_a_stop_after_a_sent_start_is_a_real_stop_that_follows_it():
    stop = decide(9, level=STOP, sent_start=True, pending_start=True)
    assert (stop.request, stop.limit, stop.verify, stop.follows_start) == (
        control.stop_request(HA),
        "stop",
        "status",
        True,
    )
    assert decide(9, level=STOP, app=oven("Run"), sent_start=True).follows_start is False
    limited = snapshot(action_free_at={"stop": 1012.0})
    assert "limit" in decide(9, level=STOP, sent_start=True, snapshot=limited).reason


def test_pause_and_resume_follow_the_state_and_the_documented_types():
    paused = decide(9, level=PAUSE, app=oven("Run"))
    assert paused.request == control.command_request(HA, catalog.PAUSE)
    assert paused.verify == "status"
    assert decide(9, level=PAUSE).reason == (
        "Pause is not possible on Oven now (Ready); it works only while Running"
    )
    assert decide(9, level=RESUME, app=oven("Pause")).key == catalog.RESUME
    dishwasher = oven("Run")
    dishwasher.type = "Dishwasher"
    assert (
        decide(9, level=PAUSE, app=dishwasher).reason
        == "Pause is not offered by Oven; use the appliance itself"
    )
    dishwasher.cache.commands.append(catalog.PAUSE)
    assert isinstance(decide(9, level=PAUSE, app=dishwasher), control.Intent)
    app = oven()
    app.cache.commands.append(catalog.OPEN_DOOR)
    door = decide(9, level=planner.ACTION_LEVELS["open_door"], app=app)
    assert (door.key, door.verify) == (catalog.OPEN_DOOR, "")


def test_action_level_zero_unknown_levels_and_other_commands_do_nothing():
    assert decide(9, level=0).reason == control.message("choose_action", "en")
    assert decide(9, level=70).reason == control.message("level_unavailable", "en")
    assert decide(9, command="On", level=10).reason == "Command On is not supported here"


def test_acknowledge_needs_the_command_in_the_appliance_list():
    ack = decide(planner.UNIT_ACK, command="On")
    assert ack.request == control.command_request(HA, catalog.ACKNOWLEDGE)
    app = oven()
    app.cache.commands = []
    assert decide(planner.UNIT_ACK, command="On", app=app).reason == (
        "Acknowledge is not offered by Oven; use the appliance itself"
    )


# -- program selector ----------------------------------------------------------------------------


def test_selecting_a_program_by_its_stable_level():
    result = decide(planner.UNIT_PROGRAM_SELECT, level=20)
    assert result.request == control.select_request(HA, OTHER)
    assert (result.overlay, result.coalesce, result.verify, result.confirm_by_get) == (
        (planner.UNIT_PROGRAM_SELECT, 20, "20"),
        "select",
        "selected",
        True,
    )


def test_program_selector_refusals():
    assert decide(8, level=0).reason == control.message("choose_program", "en")
    assert decide(8, level=40).reason == control.message("level_unavailable", "en")
    assert decide(8, level=30).reason == "Gone is not offered by Oven; choose another program"
    assert "stop it before" in decide(8, level=20, app=oven("Run")).reason


def test_a_selector_without_a_level_map_never_maps_a_level():
    state = make_state()
    state.level_maps[HA].clear()
    assert decide(8, level=10, state=state).reason == control.message("level_unavailable", "en")


# -- options and settings ------------------------------------------------------------------------


def test_setpoint_write_goes_to_the_selected_program_rounded_to_its_step():
    app = oven()
    app.options[SETPOINT] = model.Value(200, "°C")
    result = decide(SETPOINT_UNIT, level=182.0, app=app)
    assert result.request == control.option_request(HA, SETPOINT, 180, "°C")
    assert (result.overlay, result.debounce, result.verify) == (
        (SETPOINT_UNIT, 0, "180.0"),
        True,
        "selected",
    )
    assert "rounded to 180" in result.note


def test_out_of_range_is_refused_with_the_range_never_clamped():
    assert decide(SETPOINT_UNIT, level=280).reason == (
        "280 is outside the allowed range for Set temperature (30 °C to 250 °C)"
    )


DURATION = "BSH.Common.Option.Duration"
DURATION_UNIT = 24
ALARM = "BSH.Common.Setting.AlarmClock"
ALARM_UNIT = 25


def oven_with_duration():
    app = oven()
    duration = {
        "key": DURATION,
        "type": "Int",
        "unit": "seconds",
        "constraints": {"min": 1, "max": 5400},
    }
    app.cache.program_details[PROGRAM]["options"].append(duration)
    app.options[DURATION] = model.Value(60, "seconds")
    state = make_state()
    state.unit_alloc[HA][DURATION] = DURATION_UNIT
    state.unit_kinds[HA][str(DURATION_UNIT)] = "setpoint"
    return app, state


def test_a_duration_is_typed_in_minutes_and_sent_in_seconds():
    app, state = oven_with_duration()
    result = decide(DURATION_UNIT, level=1.5, app=app, state=state)
    assert result.request == control.option_request(HA, DURATION, 90, "seconds")
    assert result.overlay == (DURATION_UNIT, 0, "1.5")
    assert result.describe.endswith("1.5 min")
    half = decide(DURATION_UNIT, level=0.5, app=app, state=state)
    assert half.request == control.option_request(HA, DURATION, 30, "seconds")


def test_a_typed_duration_converts_to_whole_seconds_without_a_rounding_note():
    app, state = oven_with_duration()
    result = decide(DURATION_UNIT, level=4.1, app=app, state=state)
    assert result.request == control.option_request(HA, DURATION, 246, "seconds")
    assert result.note == ""
    third = decide(DURATION_UNIT, level=80 / 60, app=app, state=state)
    assert third.request == control.option_request(HA, DURATION, 80, "seconds")
    assert (third.overlay, third.describe.endswith("1.33 min")) == (
        (DURATION_UNIT, 0, "1.33"),
        True,
    )


def test_a_setting_reported_in_seconds_is_typed_in_minutes():
    app = oven()
    app.cache.setting_constraints[ALARM] = {"access": "readWrite", "min": 0, "max": 86340}
    app.settings[ALARM] = model.Value(0, "seconds")
    state = make_state()
    state.unit_alloc[HA][ALARM] = ALARM_UNIT
    state.unit_kinds[HA][str(ALARM_UNIT)] = "setpoint"
    result = decide(ALARM_UNIT, level=5, app=app, state=state)
    assert result.request == control.setting_request(HA, ALARM, 300)
    assert result.overlay == (ALARM_UNIT, 0, "5.0")


def test_a_duration_out_of_range_names_the_range_in_minutes():
    app, state = oven_with_duration()
    assert decide(DURATION_UNIT, level=91, app=app, state=state).reason == (
        "91 is outside the allowed range for Duration (0.02 min to 90 min)"
    )


def test_while_running_only_live_options_go_to_the_active_program():
    app = oven("Run")
    app.active_program = PROGRAM
    live = decide(SETPOINT_UNIT, level=200, app=app)
    assert live.request.path.endswith(f"/programs/active/options/{SETPOINT}")
    assert live.verify == "active"
    app.cache.program_details[PROGRAM]["options"][0]["constraints"].pop("liveupdate")
    assert decide(SETPOINT_UNIT, level=200, app=app).reason == (
        "Set temperature cannot be changed while the program runs; set it before the next start"
    )


def test_option_must_be_offered_by_the_selected_or_pending_program():
    app = oven()
    app.selected_program = OTHER
    assert decide(SETPOINT_UNIT, level=200, app=app).reason == (
        "Set temperature is not offered by the selected program; select a program that offers it"
    )
    assert isinstance(
        decide(SETPOINT_UNIT, level=200, app=app, pending_program=PROGRAM), control.Intent
    )
    app.selected_program = None
    assert "No program is selected" in decide(SETPOINT_UNIT, level=200, app=app).reason


def test_enum_option_selector_maps_levels_through_the_level_map():
    app = oven()
    spin = {"key": SPIN, "constraints": {"allowedvalues": [RPM + "800", RPM + "1400"]}}
    app.cache.program_details[PROGRAM]["options"].append(spin)
    state = make_state()
    state.unit_alloc[HA][SPIN] = SPIN_UNIT
    state.unit_kinds[HA][str(SPIN_UNIT)] = "selector"
    state.level_maps[HA][str(SPIN_UNIT)] = [RPM + "800", RPM + "1200", RPM + "1400"]
    chosen = decide(SPIN_UNIT, level=30, app=app, state=state)
    assert (chosen.value, chosen.overlay, chosen.debounce) == (
        RPM + "1400",
        (SPIN_UNIT, 30, "30"),
        False,
    )
    assert decide(SPIN_UNIT, level=20, app=app, state=state).reason == (
        "Rpm1200 is not allowed for Spin speed now; allowed: Rpm800, Rpm1400"
    )
    level_zero = decide(SPIN_UNIT, level=0, app=app, state=state)
    assert level_zero.reason == control.message("level_unavailable", "en")


def test_settings_switch_and_power():
    lock = decide(LOCK_UNIT, command="On")
    assert lock.request == control.setting_request(HA, CHILD_LOCK, True)
    assert (lock.overlay, lock.verify) == ((LOCK_UNIT, 1, "On"), "settings")
    assert decide(LOCK_UNIT, command="Toggle").reason == "Command Toggle is not supported here"
    off = decide(planner.UNIT_POWER, command="Off")
    assert (off.value, off.overlay) == (STANDBY, (planner.UNIT_POWER, 0, "Off"))
    app = oven()
    app.cache.setting_constraints[catalog.POWER_STATE]["allowedvalues"] = [ON]
    assert decide(planner.UNIT_POWER, command="Off", app=app).reason == (
        "Oven cannot be switched off remotely"
    )
    app.cache.setting_constraints.pop(catalog.POWER_STATE)
    assert "not known yet" in decide(planner.UNIT_POWER, command="On", app=app).reason
    app = oven()
    app.cache.setting_constraints[CHILD_LOCK] = {"access": "read"}
    assert (
        decide(LOCK_UNIT, command="On", app=app).reason
        == "Child lock cannot be changed from Domoticz"
    )


# -- delayed start -------------------------------------------------------------------------------


def test_delayed_start_is_held_outside_delayed_start():
    level = planner.delay_level(17 * 60)
    held = decide(planner.UNIT_DELAYED_START, level=level)
    assert held == control.Hold(17 * 60, control.message("delay_held", "en", time="17:00"))
    cleared = decide(planner.UNIT_DELAYED_START, level=0)
    assert cleared == control.Hold(None, control.message("delay_cleared", "en"))
    bad = decide(planner.UNIT_DELAYED_START, level=15)
    assert bad.reason == control.message("level_unavailable", "en")
    # Held before authorization too: nothing is sent until Start.
    assert isinstance(
        decide(planner.UNIT_DELAYED_START, level=level, authorized=False), control.Hold
    )


def test_moving_a_waiting_program_writes_its_active_option():
    app = oven("DelayedStart")
    app.active_program = PROGRAM
    app.options[catalog.START_IN] = model.Value(3600)
    held = decide(planner.UNIT_DELAYED_START, level=planner.delay_level(17 * 60), app=app)
    assert held.minutes == 17 * 60 and held.intent is not None
    expected = control.option_request(HA, catalog.START_IN, 9 * 3600, "seconds", running=True)
    assert held.intent.request == expected
    assert (held.intent.action, held.intent.verify) == ("delay", "")
    # Unit 12 shows the pending move at once, like every other write.
    level = planner.delay_level(17 * 60)
    assert held.intent.overlay == (planner.UNIT_DELAYED_START, level, str(level))
    assert decide(planner.UNIT_DELAYED_START, level=0, app=app).reason == control.message(
        "delay_now_running", "en"
    )


def waiting():
    """An oven whose program waits for its delayed start."""
    app = oven("DelayedStart")
    app.active_program = PROGRAM
    app.options[catalog.START_IN] = model.Value(3600)
    return app


def test_moving_a_waiting_program_needs_authorization_a_connection_and_a_delay_option():
    level = planner.delay_level(17 * 60)
    unit = planner.UNIT_DELAYED_START
    assert decide(unit, level=level, app=waiting(), authorized=False).reason == (
        control.message("not_authorized", "en")
    )
    app = waiting()
    app.connected = False
    assert (
        decide(unit, level=level, app=app).reason
        == "Oven is offline; check the power and Wi-Fi of the appliance"
    )
    app = waiting()
    app.options.clear()
    # Not reported by the appliance: the active program's details still name the option.
    assert isinstance(decide(unit, level=level, app=app).intent, control.Intent)
    # The waiting program refuses '-' (delay_now_running), so this names another way out.
    unknown = "The time of the waiting program is not known yet; try again shortly"
    app.active_program = OTHER
    assert decide(unit, level=level, app=app).reason == unknown
    app.active_program = None
    assert decide(unit, level=level, app=app).reason == unknown


def test_a_move_leaves_the_held_time_to_the_caller():
    state = make_state()
    state.delayed_start[HA] = 15 * 60
    held = decide(
        planner.UNIT_DELAYED_START, level=planner.delay_level(17 * 60), app=waiting(), state=state
    )
    assert held.minutes == 17 * 60
    assert state.delayed_start == {HA: 15 * 60}


def test_a_delay_shorter_than_the_option_minimum_names_the_earliest_selectable_time():
    app = oven()
    app.cache.program_details[PROGRAM]["options"][1]["constraints"]["min"] = 10 * 3600
    state = make_state()
    state.delayed_start[HA] = 17 * 60
    expected = control.message("delay_too_early", "en", time="17:00", earliest="18:00")
    assert decide(planner.UNIT_ACTIONS, level=START, app=app, state=state).reason == expected
    app.status[model.OPERATION_STATE] = model.Value(STATE + "DelayedStart")
    app.active_program = PROGRAM
    level = planner.delay_level(17 * 60)
    assert decide(planner.UNIT_DELAYED_START, level=level, app=app).reason == expected


# -- refusals every write shares -----------------------------------------------------------------


def spin_setup():
    app = oven()
    spin = {"key": SPIN, "constraints": {"allowedvalues": [RPM + "800", RPM + "1400"]}}
    app.cache.program_details[PROGRAM]["options"].append(spin)
    state = make_state()
    state.unit_alloc[HA][SPIN] = SPIN_UNIT
    state.unit_kinds[HA][str(SPIN_UNIT)] = "selector"
    state.level_maps[HA][str(SPIN_UNIT)] = [RPM + "800", RPM + "1200", RPM + "1400"]
    return app, state


@pytest.mark.parametrize(
    ("unit", "command", "level", "make"),
    [
        (SETPOINT_UNIT, "Set Level", 200, oven),
        (SPIN_UNIT, "Set Level", 30, None),
        (LOCK_UNIT, "On", 0, oven),
        (planner.UNIT_POWER, "On", 0, oven),
        (planner.UNIT_PROGRAM_SELECT, "Set Level", 20, oven),
        (planner.UNIT_ACTIONS, "Set Level", planner.ACTION_LEVELS["open_door"], door_oven),
        (planner.UNIT_ACTIONS, "Set Level", STOP, lambda: oven("Run")),
        (planner.UNIT_ACK, "On", 0, oven),
        (planner.UNIT_DELAYED_START, "Set Level", planner.delay_level(17 * 60), waiting),
    ],
)
def test_every_write_waits_out_a_block(unit, command, level, make):
    app, state = spin_setup() if make is None else (make(), make_state())
    blocked = snapshot(block_until_mono=1030.0)
    result = decide(unit, command=command, level=level, app=app, state=state, snapshot=blocked)
    assert result == control.Refusal(control.message("blocked", "en", seconds=30))


@pytest.mark.parametrize(
    ("unit", "command"),
    [
        (SETPOINT_UNIT, "On"),
        (SPIN_UNIT, "On"),
        (planner.UNIT_PROGRAM_SELECT, "On"),
        (planner.UNIT_DELAYED_START, "On"),
        (planner.UNIT_POWER, "Toggle"),
        (planner.UNIT_ACK, "Off"),
    ],
)
def test_commands_a_unit_does_not_take_are_refused(unit, command):
    app, state = spin_setup()
    result = decide(unit, command=command, level=10, app=app, state=state)
    assert result.reason == f"Command {command} is not supported here"


def test_a_setpoint_needs_a_number():
    assert decide(SETPOINT_UNIT, level="warm").reason == control.message("not_a_number", "en")


def test_an_enum_selector_turned_off_maps_no_value():
    app, state = spin_setup()
    result = decide(SPIN_UNIT, command="Off", app=app, state=state)
    assert result.reason == control.message("level_unavailable", "en")


def test_units_that_cannot_be_written_are_read_only():
    read_only = control.message("read_only", "en")
    state = make_state()
    state.unit_kinds[HA].pop(str(SETPOINT_UNIT))
    assert decide(SETPOINT_UNIT, level=200, state=state).reason == read_only
    state = make_state()
    state.unit_alloc[HA][catalog.START_IN] = 24
    assert decide(24, level=3600, state=state).reason == read_only
    state = make_state()
    state.unit_kinds[HA][str(LOCK_UNIT)] = "text"
    assert decide(LOCK_UNIT, command="On", state=state).reason == read_only


def test_an_option_the_program_reports_read_only_is_not_offered():
    app = oven()
    app.cache.program_details[PROGRAM]["options"][0]["constraints"]["access"] = "read"
    assert decide(SETPOINT_UNIT, level=200, app=app).reason == (
        "Set temperature is not offered by the selected program; select a program that offers it"
    )


def test_power_on_and_a_read_only_power_setting():
    assert decide(planner.UNIT_POWER, command="On").value == ON
    app = oven()
    app.cache.setting_constraints[catalog.POWER_STATE]["access"] = "read"
    assert (
        decide(planner.UNIT_POWER, command="On", app=app).reason
        == "Power cannot be changed from Domoticz"
    )


def test_an_active_program_without_a_reported_state_counts_as_running():
    app = oven()
    app.status.pop(model.OPERATION_STATE)
    app.active_program = PROGRAM
    assert "stop it before" in decide(planner.UNIT_PROGRAM_SELECT, level=20, app=app).reason


def test_refusals_follow_the_language():
    assert decide(planner.UNIT_ACTIONS, level=0, lang="nl").reason == control.message(
        "choose_action", "nl"
    )
    assert decide(planner.UNIT_ACTIONS, level=START, app=oven("Run"), lang="nl").reason == (
        "Oven is niet klaar om te starten (Bezig); wacht tot het Gereed is"
    )


def test_not_authorized_points_to_the_sign_in_link():
    assert decide(9, level=START, authorized=False).reason == (
        "Not signed in to Home Connect; use the sign-in link on the Home Connect device"
        " or in the Domoticz log"
    )
    assert decide(9, level=START, authorized=False, lang="nl").reason == (
        "Niet aangemeld bij Home Connect; gebruik de aanmeldlink op het Home Connect-apparaat"
        " of in het Domoticz-log"
    )


def test_a_command_dropped_by_a_new_sign_in_says_so():
    assert (
        control.message("stale", "en")
        == "Sign-in changed before it was sent; give the command again"
    )
    assert control.message("stale", "nl") == (
        "Aanmelding gewijzigd voordat het werd verstuurd; geef de opdracht opnieuw"
    )


def test_a_runtime_option_device_is_read_only_whatever_the_program_offers():
    state = make_state()
    forecast = "Cooking.Oven.Option.EstimatedTotalProgramTime"
    state.unit_alloc[HA][forecast] = 24
    state.unit_kinds[HA]["24"] = "minutes"
    assert decide(24, level=600, state=state).reason == control.message("read_only", "en")


def test_an_option_while_a_program_runs_that_is_not_known_yet():
    app = oven("Run")
    app.active_program = None
    assert decide(SETPOINT_UNIT, level=200, app=app).reason == (
        "The program running on Oven is not known yet; try again shortly"
    )


# -- unchanged values ----------------------------------------------------------------------------


def skipped(result, command):
    return result == control.Refusal(
        control.message("unchanged", "en", command=command), "debug", "command_skipped"
    )


def reported_for_the_program(app, root=model.SELECTED_ROOT):
    """The setpoint was reported after the program it belongs to was."""
    app.seq[root], app.seq[SETPOINT] = 1, 2
    return app


def test_a_value_already_observed_is_not_sent():
    # No request and no budget: even a block in force does not matter.
    blocked = snapshot(block_until_mono=1030.0)
    app = reported_for_the_program(oven())
    result = decide(SETPOINT_UNIT, level=182.0, app=app, snapshot=blocked)
    assert skipped(result, "Set temperature: 180 °C")
    running = reported_for_the_program(oven("Run"), model.ACTIVE_ROOT)
    running.active_program = PROGRAM
    assert skipped(decide(SETPOINT_UNIT, level=180, app=running), "Set temperature: 180 °C")
    app = oven()
    app.settings[CHILD_LOCK] = model.Value(True)
    app.settings[catalog.POWER_STATE] = model.Value(ON)
    assert skipped(decide(LOCK_UNIT, command="On", app=app), "Child lock: On")
    assert skipped(decide(planner.UNIT_POWER, command="On", app=app), "Power: On")
    assert isinstance(decide(LOCK_UNIT, command="Off", app=app), control.Intent)
    assert isinstance(decide(planner.UNIT_POWER, command="Off", app=app), control.Intent)


def test_an_observed_value_is_sent_while_a_write_to_it_or_a_selection_is_pending():
    # A pending write may still change the value, and a pending selection brings the values of
    # another program, so the observed one says nothing about the outcome.
    app = reported_for_the_program(oven())
    assert isinstance(decide(SETPOINT_UNIT, level=185, app=app), control.Intent)
    pending = frozenset({SETPOINT})
    result = decide(SETPOINT_UNIT, level=180, app=app, pending_keys=pending)
    assert isinstance(result, control.Intent)
    result = decide(SETPOINT_UNIT, level=180, app=app, pending_program=PROGRAM)
    assert isinstance(result, control.Intent)
    app = oven()
    app.settings[CHILD_LOCK] = model.Value(True)
    lock = decide(LOCK_UNIT, command="On", app=app, pending_keys=frozenset({CHILD_LOCK}))
    assert isinstance(lock, control.Intent)


def test_an_option_value_left_from_another_program_is_sent():
    # The model still holds the value the previous program reported: it says nothing about
    # the program selected since, so the write goes.
    app = oven()
    app.seq[SETPOINT], app.seq[model.SELECTED_ROOT] = 1, 2
    assert isinstance(decide(SETPOINT_UNIT, level=180, app=app), control.Intent)
    assert isinstance(decide(SETPOINT_UNIT, level=180, app=oven()), control.Intent)
    running = oven("Run")
    running.active_program = PROGRAM
    running.seq[model.SELECTED_ROOT], running.seq[SETPOINT], running.seq[model.ACTIVE_ROOT] = (
        1,
        2,
        3,
    )
    assert isinstance(decide(SETPOINT_UNIT, level=180, app=running), control.Intent)


# -- findings from the real-appliance control tests ----------------------------------------------


@pytest.mark.parametrize("power", [STANDBY, "BSH.Common.EnumType.PowerState.Off"])
def test_a_program_is_not_chosen_on_an_appliance_reported_off(power):
    app = oven("Inactive")
    app.settings[catalog.POWER_STATE] = model.Value(power)
    assert decide(planner.UNIT_PROGRAM_SELECT, level=20, app=app).reason == (
        "Oven is switched off; switch it on first"
    )


def test_a_program_is_chosen_when_the_power_is_on_or_not_reported():
    app = oven()
    app.settings[catalog.POWER_STATE] = model.Value(ON)
    assert isinstance(decide(planner.UNIT_PROGRAM_SELECT, level=20, app=app), control.Intent)
    assert isinstance(decide(planner.UNIT_PROGRAM_SELECT, level=20), control.Intent)


def test_start_is_refused_while_the_door_is_reported_open():
    app = oven()
    app.status[catalog.DOOR_STATE] = model.Value("BSH.Common.EnumType.DoorState.Open")
    reason = decide(planner.UNIT_ACTIONS, level=START, app=app).reason
    assert reason == errors.user_message("SDK.Error.WrongDoorState", "Oven", "en")
    for closed in ("Closed", "Locked"):
        app.status[catalog.DOOR_STATE] = model.Value(f"BSH.Common.EnumType.DoorState.{closed}")
        assert isinstance(decide(planner.UNIT_ACTIONS, level=START, app=app), control.Intent)


def test_door_actions_need_the_command_in_the_appliance_list():
    door = planner.ACTION_LEVELS["open_door"]
    assert (
        decide(9, level=door).reason == "Open door is not offered by Oven; use the appliance itself"
    )
    app = oven()
    app.cache.commands.append(catalog.OPEN_DOOR)
    assert decide(9, level=door, app=app).key == catalog.OPEN_DOOR


def test_a_program_is_chosen_behind_a_power_on_not_confirmed_yet():
    # A scene's "Power On, Program choice, Start" in one go: the selection is judged while the
    # Power On write is still pending.
    app = oven("Inactive")
    app.settings[catalog.POWER_STATE] = model.Value(STANDBY)
    pending = frozenset({catalog.POWER_STATE})
    result = decide(planner.UNIT_PROGRAM_SELECT, level=20, app=app, pending_keys=pending)
    assert isinstance(result, control.Intent)


def test_the_operation_state_names_its_fix_before_an_open_door():
    app = oven("Finished")
    app.status[catalog.DOOR_STATE] = model.Value("BSH.Common.EnumType.DoorState.Open")
    assert decide(planner.UNIT_ACTIONS, level=START, app=app).reason == (
        "Oven is not ready to start (Finished); open the door or acknowledge the end first"
    )
