import json

import model
import sse

HA = "SIEMENS-HCS01OVN1-000000000001"
OP = "BSH.Common.Status.OperationState"
SELECTED = "BSH.Common.Root.SelectedProgram"
ACTIVE = "BSH.Common.Root.ActiveProgram"


def listing(*ids, connected=True):
    return {
        "data": {
            "homeappliances": [
                {
                    "haId": i,
                    "name": f"Oven {n}",
                    "type": "Oven",
                    "brand": "SIEMENS",
                    "vib": "X",
                    "connected": connected,
                }
                for n, i in enumerate(ids)
            ]
        }
    }


def status(state="Ready", door="Closed"):
    return {
        "data": {
            "status": [
                {
                    "key": OP,
                    "value": f"BSH.Common.EnumType.OperationState.{state}",
                    "name": "Operation state",
                    "displayvalue": state,
                },
                {
                    "key": "BSH.Common.Status.DoorState",
                    "value": f"BSH.Common.EnumType.DoorState.{door}",
                    "name": "Door",
                },
            ]
        }
    }


def event(kind, items, ha=HA):
    return sse.Event(kind, json.dumps({"haId": ha, "items": items}), ha)


def ready_model():
    m = model.Model()
    m.apply_list(listing(HA), m.tick())
    m.apply_status(HA, status(), m.tick())
    return m


def test_list_creates_appliances_and_skips_invalid_ids():
    m = model.Model()
    new = m.apply_list(listing(HA, "../bad"), m.tick())
    assert new == [HA]
    app = m.appliances[HA]
    assert (app.name, app.type, app.connected) == ("Oven 0", "Oven", True)


def test_status_and_state_helpers():
    m = ready_model()
    app = m.appliances[HA]
    assert app.operation_state() == "Ready"
    assert app.is_idle()
    assert app.names[OP] == "Operation state"


def test_old_response_does_not_roll_back_newer_event():
    m = ready_model()
    issued = m.tick()  # a GET is issued now
    m.apply_event(event("STATUS", [{"key": OP, "value": "BSH.Common.EnumType.OperationState.Run"}]))
    m.apply_status(HA, status("Ready"), issued)  # the stale answer arrives later
    assert m.appliances[HA].operation_state() == "Run"


def test_newer_response_does_apply():
    m = ready_model()
    m.apply_event(event("STATUS", [{"key": OP, "value": "BSH.Common.EnumType.OperationState.Run"}]))
    m.apply_status(HA, status("Finished"), m.tick())
    assert m.appliances[HA].operation_state() == "Finished"


def test_notify_routes_options_settings_and_program_changes():
    m = ready_model()
    eff = m.apply_event(
        event(
            "NOTIFY",
            [
                {"key": "BSH.Common.Option.RemainingProgramTime", "value": 600, "unit": "seconds"},
                {"key": "BSH.Common.Setting.ChildLock", "value": True},
                {
                    "key": "BSH.Common.Root.SelectedProgram",
                    "value": "Cooking.Oven.Program.HeatingMode.HotAir",
                },
            ],
        )
    )
    app = m.appliances[HA]
    assert app.options["BSH.Common.Option.RemainingProgramTime"].value == 600
    assert app.settings["BSH.Common.Setting.ChildLock"].value is True
    assert app.selected_program == "Cooking.Oven.Program.HeatingMode.HotAir"
    assert eff.program_changed == "selected"
    assert eff.unknown_settings == ("BSH.Common.Setting.ChildLock",)


def test_none_selected_program_is_no_change_to_fetch():
    m = ready_model()
    eff = m.apply_event(
        event("NOTIFY", [{"key": "BSH.Common.Root.SelectedProgram", "value": None}])
    )
    assert m.appliances[HA].selected_program is None
    assert eff.program_changed is None


def test_runtime_option_never_counts_as_unknown():
    m = ready_model()
    eff = m.apply_event(
        event("NOTIFY", [{"key": "LaundryCare.Common.Option.ProcessPhase", "value": "x"}])
    )
    assert eff.unknown_settings == ()


def test_events_and_connection_events():
    m = ready_model()
    m.apply_event(
        event(
            "EVENT",
            [
                {
                    "key": "BSH.Common.Event.ProgramFinished",
                    "value": model.PRESENT,
                    "level": "hint",
                    "handling": "acknowledge",
                }
            ],
        )
    )
    assert m.appliances[HA].events["BSH.Common.Event.ProgramFinished"].level == "hint"
    assert m.apply_event(sse.Event("DISCONNECTED", "", HA)).kind == "disconnected"
    assert m.appliances[HA].connected is False
    assert m.apply_event(sse.Event("CONNECTED", "", HA)).kind == "connected"
    assert m.appliances[HA].connected is True
    assert m.apply_event(sse.Event("KEEP-ALIVE", "", "")).kind == "keepalive"


def test_event_for_unknown_appliance():
    m = ready_model()
    eff = m.apply_event(event("STATUS", [{"key": OP, "value": "x"}], ha="BOSCH-NEW-1"))
    assert eff.unknown_appliance and eff.ha_id == "BOSCH-NEW-1"


def test_program_list_is_append_only_and_only_from_idle_answers():
    m = ready_model()
    app = m.appliances[HA]
    full = {"data": {"programs": [{"key": "P.A", "name": "A"}, {"key": "P.B", "name": "B"}]}}
    short = {"data": {"programs": [{"key": "P.A", "name": "A"}]}}
    m.apply_available(HA, full, state_stamp="Ready")
    m.apply_available(HA, short, state_stamp="Ready")
    assert app.cache.programs == ["P.A", "P.B"]
    m.apply_available(HA, {"data": {"programs": [{"key": "P.C", "name": "C"}]}}, state_stamp="Run")
    assert "P.C" not in app.cache.programs


def test_program_payload_sets_options_and_names():
    m = ready_model()
    payload = {
        "data": {
            "key": "P.A",
            "name": "4D Hot air",
            "options": [
                {
                    "key": "Cooking.Oven.Option.SetpointTemperature",
                    "value": 180,
                    "unit": "°C",
                    "name": "Cavity temperature",
                },
            ],
        }
    }
    m.apply_program(HA, "active", payload, m.tick())
    app = m.appliances[HA]
    assert app.active_program == "P.A"
    assert app.names["P.A"] == "4D Hot air"
    assert app.options["Cooking.Oven.Option.SetpointTemperature"].value == 180
    m.apply_program(HA, "active", None, m.tick())
    assert app.active_program is None


def test_capabilities():
    m = ready_model()
    app = m.appliances[HA]
    m.apply_program_detail(
        HA,
        "P.A",
        {
            "data": {
                "key": "P.A",
                "options": [
                    {
                        "key": "Cooking.Oven.Option.SetpointTemperature",
                        "type": "Double",
                        "constraints": {"min": 30, "max": 250},
                    },
                ],
            }
        },
    )
    m.apply_setting_detail(
        HA,
        "BSH.Common.Setting.ChildLock",
        {"data": {"key": "BSH.Common.Setting.ChildLock", "constraints": {"access": "readWrite"}}},
    )
    m.apply_setting_detail(
        HA,
        "BSH.Common.Setting.TemperatureUnit",
        {"data": {"key": "BSH.Common.Setting.TemperatureUnit", "constraints": {"access": "read"}}},
    )
    assert app.capability("Cooking.Oven.Option.SetpointTemperature") == "selectable"
    assert app.capability("BSH.Common.Option.RemainingProgramTime") == "readonly"
    assert app.capability("BSH.Common.Option.StartInRelative") == "start_only"
    assert app.capability("BSH.Common.Setting.ChildLock") == "setting_rw"
    assert app.capability("BSH.Common.Setting.TemperatureUnit") == "setting_ro"
    assert app.capability("BSH.Common.Setting.Other") == "setting_unknown"


def test_garbage_payloads_do_not_raise():
    m = ready_model()
    for bad in [None, {}, {"data": "x"}, {"data": {"status": "x"}}, {"data": {"status": [1, "a"]}}]:
        m.apply_status(HA, bad, m.tick())
        m.apply_settings(HA, bad, m.tick())
        m.apply_available(HA, bad, "Ready")
    m.apply_status("NOT-KNOWN", status(), m.tick())
    assert m.apply_event(sse.Event("STATUS", "not json", HA)).kind == "status"


def detail(key, low, high, access=None):
    constraints = {"min": low, "max": high}
    if access:
        constraints["access"] = access
    return {
        "data": {
            "key": key,
            "options": [
                {
                    "key": "Cooking.Oven.Option.SetpointTemperature",
                    "type": "Double",
                    "constraints": constraints,
                },
            ],
        }
    }


SETPOINT = "Cooking.Oven.Option.SetpointTemperature"


def test_option_detail_prefers_selected_then_active_then_any():
    m = ready_model()
    app = m.appliances[HA]
    m.apply_program_detail(HA, "P.A", detail("P.A", 30, 250))
    m.apply_program_detail(HA, "P.B", detail("P.B", 30, 275))
    m.apply_program_detail(HA, "P.C", detail("P.C", 40, 200))
    assert model.option_detail(app, SETPOINT)["constraints"]["max"] == 250  # first in dict order
    app.active_program = "P.C"
    assert model.option_detail(app, SETPOINT)["constraints"]["max"] == 200
    app.selected_program = "P.B"
    assert model.option_detail(app, SETPOINT)["constraints"]["max"] == 275
    assert model.option_detail(app, "No.Such.Option") is None


def test_capability_uses_the_selected_programs_constraints():
    m = ready_model()
    app = m.appliances[HA]
    m.apply_program_detail(HA, "P.A", detail("P.A", 30, 250, access="read"))
    m.apply_program_detail(HA, "P.B", detail("P.B", 30, 275))
    assert app.capability(SETPOINT) == "readonly"  # fallback: first cached detail
    app.selected_program = "P.B"
    assert app.capability(SETPOINT) == "selectable"


def test_running_appliance_with_short_list_never_shrinks_programs_or_options():
    m = ready_model()
    app = m.appliances[HA]
    full = {"data": {"programs": [{"key": "P.A"}, {"key": "P.B"}, {"key": "P.C"}]}}
    m.apply_available(HA, full, state_stamp="Ready")
    m.apply_program_detail(HA, "P.A", detail("P.A", 30, 250))
    m.apply_program(
        HA,
        "active",
        {
            "data": {
                "key": "P.A",
                "options": [
                    {"key": SETPOINT, "value": 180},
                ],
            }
        },
        m.tick(),
    )
    cotton_only = {"data": {"programs": [{"key": "P.A"}]}}
    for stamp in ("Run", "Finished", "Pause"):
        m.apply_available(HA, cotton_only, state_stamp=stamp)
    m.apply_available(HA, {"data": {"programs": []}}, state_stamp="Ready")
    assert app.cache.programs == ["P.A", "P.B", "P.C"]
    assert SETPOINT in app.options
    assert model.option_detail(app, SETPOINT) is not None


def test_invalid_keys_and_ids_in_payloads_and_events_are_skipped():
    m = ready_model()
    app = m.appliances[HA]
    bad = "../x"
    m.apply_status(HA, {"data": {"status": [{"key": bad, "value": 1}]}}, m.tick())
    m.apply_available(HA, {"data": {"programs": [{"key": bad}, {"key": 7}]}}, "Ready")
    m.apply_commands(HA, {"data": {"commands": [{"key": bad}]}})
    m.apply_program(HA, "selected", {"data": {"key": bad, "options": [{"key": bad}]}}, m.tick())
    m.apply_program_detail(HA, bad, {"data": {"key": bad}})
    m.apply_setting_detail(HA, bad, {"data": {"key": bad}})
    m.apply_event(event("NOTIFY", [{"key": bad, "value": 1}, {"key": SELECTED, "value": bad}]))
    m.apply_event(event("STATUS", [{"key": OP, "value": "x"}], ha=bad))
    assert bad not in app.status and bad not in app.options and bad not in app.names
    assert app.cache.programs == [] and app.cache.commands == []
    assert app.cache.program_details == {} and app.cache.setting_constraints == {}
    assert app.selected_program is None
    assert bad not in m.appliances


def test_numeric_ha_id_is_accepted_and_uri_style_ids_validated():
    m = model.Model()
    assert m.apply_list(listing("123456789012345678"), m.tick()) == ["123456789012345678"]
    eff = m.apply_event(event("STATUS", [{"key": OP, "value": "x"}], ha="123456789012345678"))
    assert not eff.unknown_appliance


def test_deeply_nested_event_data_does_not_raise():
    m = ready_model()
    eff = m.apply_event(sse.Event("NOTIFY", "[" * 200_000, HA))
    assert eff.kind == "notify"


def program(key):
    return {"data": {"key": key}}


def test_stale_active_404_does_not_clear_newer_active_event():
    m = ready_model()
    issued = m.tick()
    m.apply_event(event("NOTIFY", [{"key": ACTIVE, "value": "P.A"}]))
    m.apply_program(HA, "active", None, issued)
    assert m.appliances[HA].active_program == "P.A"


def test_stale_selected_answer_does_not_overwrite_newer_event():
    m = ready_model()
    issued = m.tick()
    m.apply_event(event("NOTIFY", [{"key": SELECTED, "value": "P.B"}]))
    m.apply_program(HA, "selected", program("P.A"), issued)
    assert m.appliances[HA].selected_program == "P.B"


def test_fresh_program_answers_apply():
    m = ready_model()
    m.apply_event(event("NOTIFY", [{"key": ACTIVE, "value": "P.A"}]))
    m.apply_program(HA, "active", None, m.tick())
    m.apply_program(HA, "selected", program("P.B"), m.tick())
    app = m.appliances[HA]
    assert app.active_program is None
    assert app.selected_program == "P.B"


def test_stale_list_does_not_reverse_newer_disconnected_or_connected():
    m = ready_model()
    issued = m.tick()
    m.apply_event(sse.Event("DISCONNECTED", "", HA))
    m.apply_list(listing(HA, connected=True), issued)
    assert m.appliances[HA].connected is False
    m.apply_list(listing(HA, connected=True), m.tick())
    assert m.appliances[HA].connected is True
    issued = m.tick()
    m.apply_event(sse.Event("CONNECTED", "", HA))
    m.apply_event(sse.Event("DISCONNECTED", "", HA))
    m.apply_event(sse.Event("CONNECTED", "", HA))
    m.apply_list(listing(HA, connected=False), issued)
    assert m.appliances[HA].connected is True


def test_fresh_list_applies_after_disconnected_event():
    m = ready_model()
    m.apply_event(sse.Event("DISCONNECTED", "", HA))
    m.apply_list(listing(HA, connected=True), m.tick())
    assert m.appliances[HA].connected is True


REMAINING = "BSH.Common.Option.RemainingProgramTime"


def test_observed_values_record_their_receive_time():
    clock = [1000.0]
    m = model.Model(wall=lambda: clock[0])
    m.apply_list(listing(HA), m.tick())
    m.apply_event(event("NOTIFY", [{"key": REMAINING, "value": 600, "unit": "seconds"}]))
    assert m.appliances[HA].received[REMAINING] == 1000.0
    clock[0] = 1060.0
    m.apply_status(HA, status("Run"), m.tick())
    assert m.appliances[HA].received[OP] == 1060.0


def test_stale_answer_keeps_the_newer_receive_time():
    clock = [1000.0]
    m = model.Model(wall=lambda: clock[0])
    m.apply_list(listing(HA), m.tick())
    issued = m.tick()  # a GET is issued now
    clock[0] = 1010.0
    m.apply_event(event("NOTIFY", [{"key": REMAINING, "value": 590, "unit": "seconds"}]))
    clock[0] = 1020.0  # the stale answer arrives later, carrying the older countdown
    m.apply_program(
        HA,
        "active",
        {"data": {"key": "P.A", "options": [{"key": REMAINING, "value": 600}]}},
        issued,
    )
    app = m.appliances[HA]
    assert (app.options[REMAINING].value, app.received[REMAINING]) == (590, 1010.0)


def _answer_model() -> model.Model:
    m = model.Model(wall=lambda: 1000.0)
    m.apply_list(listing(HA), m.tick())
    return m


def test_answer_dispatch_applies_each_kind():
    m = _answer_model()
    app = m.appliances[HA]
    m.apply_answer("status", HA, 200, status("Ready"), issued=m.tick())
    assert app.operation_state() == "Ready"
    settings = {"data": {"settings": [{"key": "BSH.Common.Setting.PowerState", "value": "On"}]}}
    m.apply_answer("settings", HA, 200, settings, issued=m.tick())
    assert "BSH.Common.Setting.PowerState" in app.settings
    programs = {"data": {"programs": [{"key": "Cooking.Oven.Program.HeatingMode.HotAir"}]}}
    m.apply_answer("available", HA, 200, programs, state_stamp="Ready")
    assert app.cache.programs == ["Cooking.Oven.Program.HeatingMode.HotAir"]
    detail = {"data": {"key": "Cooking.Oven.Program.HeatingMode.HotAir", "options": []}}
    key = "Cooking.Oven.Program.HeatingMode.HotAir"
    m.apply_answer("program_detail", HA, 200, detail, key=key)
    assert key in app.cache.program_details
    selected = {"data": {"key": key}}
    m.apply_answer("selected", HA, 200, selected, issued=m.tick())
    assert app.selected_program == key
    m.apply_answer("selected", HA, 404, None, issued=m.tick())
    assert app.selected_program is None
    commands = {"data": {"commands": [{"key": "BSH.Common.Command.PauseProgram"}]}}
    m.apply_answer("commands", HA, 200, commands)
    assert app.cache.commands == ["BSH.Common.Command.PauseProgram"]


def test_answer_dispatch_ignores_failures_except_definite_setting_refusals():
    m = _answer_model()
    app = m.appliances[HA]
    for kind in ("status", "settings", "available", "program_detail", "commands"):
        m.apply_answer(kind, HA, 409, None, key="BSH.Common.Setting.PowerState")
    m.apply_answer("active", HA, 500, None, issued=m.tick())
    assert app.status == {} and app.settings == {} and app.cache.programs == []
    assert app.cache.program_details == {} and app.cache.commands == []
    power, lock, light = (
        "BSH.Common.Setting.PowerState",
        "BSH.Common.Setting.ChildLock",
        "Cooking.Common.Setting.Lighting",
    )
    rw = {"data": {"key": power, "constraints": {"access": "readWrite"}}}
    m.apply_answer("setting_detail", HA, 200, rw, key=power)
    m.apply_answer("setting_detail", HA, 403, None, key=lock)
    # 409 is an offline or state answer, not a verdict on the setting: asked again later.
    for transient in (408, 409, 429, 500, 0):
        m.apply_answer("setting_detail", HA, transient, None, key=light)
    assert app.cache.setting_constraints == {power: {"access": "readWrite"}, lock: {}}
    assert app.capability(power) == "setting_rw"
    assert app.capability(lock) == "setting_ro"
    assert app.capability(light) == "setting_unknown"


ITEM = {"key": OP, "value": "BSH.Common.EnumType.OperationState.Run"}
EVENT_ITEM = {"key": "BSH.Common.Event.ProgramFinished", "value": model.PRESENT}


def test_any_stream_item_brings_back_an_appliance_a_rest_answer_marked_offline():
    for kind, item in (("STATUS", ITEM), ("NOTIFY", ITEM), ("EVENT", EVENT_ITEM)):
        m = ready_model()
        m.set_offline(HA)
        app = m.appliances[HA]
        assert not app.connected
        m.apply_event(event(kind, [item]))
        assert app.connected, kind
        assert app.resync_due, kind  # the offline answer left its fetch undone


def test_replayed_or_power_off_items_after_a_disconnect_leave_it_offline():
    # EVENT items are replayed on every stream connect (spec 3.3), and an appliance switching
    # off sends SelectedProgram = None: neither says it is reachable again.
    for kind, items in (
        ("EVENT", [EVENT_ITEM]),
        ("NOTIFY", [{"key": SELECTED, "value": None}]),
    ):
        m = ready_model()
        m.apply_event(sse.Event("DISCONNECTED", "", HA))
        m.apply_event(event(kind, items))
        app = m.appliances[HA]
        assert not app.connected and not app.resync_due, kind


def test_live_items_revive_an_appliance_whose_connected_event_was_missed():
    live = (
        ("STATUS", [ITEM]),
        ("NOTIFY", [{"key": "BSH.Common.Option.ProgramProgress", "value": 40}]),
    )
    for kind, items in live:
        m = ready_model()
        m.apply_event(sse.Event("DISCONNECTED", "", HA))
        m.apply_event(event(kind, items))
        app = m.appliances[HA]
        assert app.connected and app.resync_due, kind
        listed = model.Model()
        listed.apply_list(listing(HA, connected=False), listed.tick())
        listed.apply_event(event(kind, items))
        assert listed.appliances[HA].connected and listed.appliances[HA].resync_due, kind


def test_item_without_a_valid_key_does_not_reconnect():
    m = ready_model()
    m.set_offline(HA)
    m.apply_event(event("STATUS", [{"key": "../bad", "value": 1}]))
    assert not m.appliances[HA].connected


def test_success_answer_reconnects_unless_it_predates_the_offline_mark():
    m = ready_model()
    stale = m.tick()  # issued before the offline answer arrived
    m.set_offline(HA)
    app = m.appliances[HA]
    m.apply_answer("settings", HA, 200, {"data": {"settings": []}}, issued=stale)
    assert not app.connected
    m.apply_answer("selected", HA, 404, None, issued=m.tick())
    assert not app.connected  # only a success proves the appliance answers
    m.apply_answer("settings", HA, 200, {"data": {"settings": []}}, issued=m.tick())
    assert app.connected and app.resync_due


def test_connected_event_and_new_offline_clear_the_resync():
    m = ready_model()
    m.set_offline(HA)
    m.apply_event(event("STATUS", [ITEM]))
    m.apply_event(sse.Event("CONNECTED", "", HA))
    assert not m.appliances[HA].resync_due  # CONNECTED refetches on its own
    m.set_offline(HA)
    m.apply_event(event("STATUS", [ITEM]))
    m.set_offline(HA)
    assert not m.appliances[HA].resync_due


def test_listing_connected_after_an_offline_answer_asks_for_a_resync():
    m = ready_model()
    m.set_offline(HA)
    m.apply_list(listing(HA), m.tick())
    app = m.appliances[HA]
    assert app.connected and app.resync_due
    m.set_offline(HA)
    m.apply_list(listing(HA, connected=False), m.tick())
    assert not app.connected and not app.resync_due
