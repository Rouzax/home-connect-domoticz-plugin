import json
import time
from dataclasses import replace

import catalog
import domoticz_api
import model
import persistence
import planner
import sse

HA = "SIEMENS-HCS01OVN1-000000000001"
NOW = 1_800_000_000.0  # 2027-01-15 08:00 UTC; tests compare relative text only
SPIN = "LaundryCare.Washer.Option.SpinSpeed"
SETPOINT = "Cooking.Oven.Option.SetpointTemperature"


def app_with(**status):
    app = model.Appliance(HA, name="Oven 1", brand="SIEMENS", type="Oven", connected=True)
    for key, value in status.items():
        app.status[key] = model.Value(value)
    return app


def by_unit(updates, dev=HA):
    return {u.unit: u for u in updates if u.device_id == dev}


def plan(app, state=None, stale=False, lang="en"):
    state = state or persistence.State()
    return state, by_unit(planner.plan_appliance(app, state, lang, NOW, stale, app.name))


def dynamic(units):
    return {u.name.removeprefix("Oven 1 "): u for n, u in units.items() if n >= 20}


def test_state_alert_colour_and_text():
    app = app_with(**{catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.ActionRequired"})
    app.active_program = "P.A"
    app.names["P.A"] = "4D Hot air"
    _, units = plan(app)
    u = units[planner.UNIT_STATE]
    assert (u.type_name, u.nvalue) == ("Alert", catalog.ORANGE)
    assert u.svalue == "Action required, 4D Hot air"
    assert u.name == "Oven 1 State"


def test_offline_and_last_known():
    app = app_with(**{catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.Run"})
    _, units = plan(app, stale=True)
    assert units[planner.UNIT_STATE].svalue.endswith("(last known)")
    app.connected = False
    _, units = plan(app)
    assert (units[planner.UNIT_STATE].nvalue, units[planner.UNIT_STATE].svalue) == (
        catalog.GREY,
        "Offline",
    )


def test_power_and_door():
    app = app_with(
        **{
            catalog.DOOR_STATE: "BSH.Common.EnumType.DoorState.Locked",
            catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.Run",
        }
    )
    app.settings[catalog.POWER_STATE] = model.Value("BSH.Common.EnumType.PowerState.Standby")
    _, units = plan(app)
    assert (units[planner.UNIT_POWER].nvalue, units[planner.UNIT_POWER].svalue) == (0, "Off")
    door = units[planner.UNIT_DOOR]
    assert (door.switchtype, door.nvalue) == (11, 0)
    assert "Locked" in units[planner.UNIT_STATE].svalue
    app.settings[catalog.POWER_STATE] = model.Value("BSH.Common.EnumType.PowerState.On")
    app.status[catalog.DOOR_STATE] = model.Value("BSH.Common.EnumType.DoorState.Open")
    _, units = plan(app)
    assert (units[planner.UNIT_POWER].nvalue, units[planner.UNIT_POWER].svalue) == (1, "On")
    assert (units[planner.UNIT_DOOR].nvalue, units[planner.UNIT_DOOR].svalue) == (1, "On")


def test_dynamic_switch_and_door_share_the_fixed_logic():
    app = app_with(**{"Refrigeration.FridgeFreezer.Status.Door.Freezer": "X.DoorState.Open"})
    app.settings["BSH.Common.Setting.ChildLock"] = model.Value(True)
    app.cache.setting_constraints["BSH.Common.Setting.ChildLock"] = {"access": "readWrite"}
    _, units = plan(app)
    dyn = dynamic(units)
    child = dyn["Child lock"]
    assert (child.type_name, child.nvalue, child.svalue) == ("Switch", 1, "On")
    door = dyn["Freezer"]
    assert (door.switchtype, door.nvalue, door.svalue) == (11, 1, "On")


def test_no_values_means_no_fabricated_updates():
    _, units = plan(app_with())
    assert planner.UNIT_PROGRESS not in units
    assert planner.UNIT_TIME not in units
    assert planner.UNIT_DOOR not in units


def test_remaining_time_and_finish_text():
    app = app_with(**{catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.Run"})
    app.active_program = "P.A"
    app.names["P.A"] = "Cotton"
    app.options[catalog.REMAINING] = model.Value(3059, "seconds")
    app.options[catalog.ESTIMATED] = model.Value(True)
    app.options[catalog.PROGRESS] = model.Value(87, "%")
    _, units = plan(app)
    assert units[planner.UNIT_TIME].svalue == "51"
    assert units[planner.UNIT_TIME].options == {"Custom": "1;min"}
    assert units[planner.UNIT_PROGRESS].svalue == "87"
    assert units[planner.UNIT_PROGRAM].svalue.startswith("Cotton, ready at about ")


def test_clock_is_local_hours_and_minutes():
    assert planner.clock(NOW) == time.strftime("%H:%M", time.localtime(NOW))


def test_remote_control_alert():
    app = app_with(
        **{catalog.REMOTE_ACTIVE: True, catalog.REMOTE_START: True, catalog.LOCAL_CONTROL: False}
    )
    _, units = plan(app)
    assert (units[planner.UNIT_REMOTE].nvalue, units[planner.UNIT_REMOTE].svalue) == (
        catalog.GREEN,
        "Remote start allowed",
    )
    app.status[catalog.LOCAL_CONTROL] = model.Value(True)
    _, units = plan(app)
    assert units[planner.UNIT_REMOTE].nvalue == catalog.ORANGE


def oven_with_details():
    app = app_with()
    app.cache.programs = ["P.A"]
    app.cache.program_details["P.A"] = {
        "options": [
            {
                "key": SETPOINT,
                "type": "Double",
                "unit": "°C",
                "constraints": {"min": 30, "max": 250, "stepsize": 5},
            },
            {
                "key": SPIN,
                "type": "LaundryCare.Washer.EnumType.SpinSpeed",
                "constraints": {
                    "allowedvalues": [
                        "LaundryCare.Washer.EnumType.SpinSpeed.RPM800",
                        "LaundryCare.Washer.EnumType.SpinSpeed.RPM1200",
                    ]
                },
            },
        ]
    }
    app.selected_program = "P.A"
    app.options[SETPOINT] = model.Value(180.0, "°C")
    app.options[SPIN] = model.Value("LaundryCare.Washer.EnumType.SpinSpeed.RPM1200")
    app.options["BSH.Common.Option.EnergyForecast"] = model.Value(61, "%")
    app.options["BSH.Common.Option.StartInRelative"] = model.Value(0, "seconds")
    app.value_names["LaundryCare.Washer.EnumType.SpinSpeed.RPM1200"] = "1200 rpm"
    return app


def test_dynamic_option_devices_and_kinds():
    state, units = plan(oven_with_details())
    dyn = dynamic(units)
    setpoint = dyn["Set temperature"]
    assert setpoint.type_name == "Set Point"
    plain = {k: v for k, v in setpoint.options.items() if not k.startswith("HC")}
    assert plain == {"ValueMin": "30", "ValueMax": "250", "ValueStep": "5", "ValueUnit": "°C"}
    assert (setpoint.options["HCKey"], setpoint.options["HCKind"]) == (SETPOINT, "setpoint")
    assert setpoint.svalue == "180.0"
    spin = dyn["Spin speed"]
    assert (spin.type_name, spin.switchtype) == ("Selector Switch", 18)
    assert spin.options["LevelNames"] == "Not available|Rpm800|1200 rpm"
    assert (
        spin.options["HCKeys"] == "|LaundryCare.Washer.EnumType.SpinSpeed.RPM800"
        "|LaundryCare.Washer.EnumType.SpinSpeed.RPM1200"
    )
    assert (spin.nvalue, spin.svalue) == (20, "20")
    assert dyn["Energy forecast"].type_name == "Percentage"  # read-only runtime value
    assert "Start in relative" not in dyn  # start-only: control plan
    assert state.unit_kinds[HA][str(setpoint.unit)] == "setpoint"


def test_setpoint_range_follows_the_selected_program():
    app = oven_with_details()
    app.cache.programs.append("P.B")
    app.cache.program_details["P.B"] = {
        "options": [
            {
                "key": SETPOINT,
                "type": "Double",
                "unit": "°C",
                "constraints": {"min": 100, "max": 300, "stepsize": 1},
            }
        ]
    }
    _, units = plan(app)
    assert dynamic(units)["Set temperature"].options["ValueMax"] == "250"
    app.selected_program = "P.B"
    _, units = plan(app)
    setpoint = dynamic(units)["Set temperature"]
    assert (setpoint.options["ValueMin"], setpoint.options["ValueMax"]) == ("100", "300")


def test_option_offered_only_by_another_program_gets_a_device():
    app = oven_with_details()
    app.cache.programs.append("P.B")
    app.cache.program_details["P.B"] = {
        "options": [
            {"key": "Cooking.Oven.Option.FastPreHeat", "type": "Boolean"},
            {"key": "BSH.Common.Option.FinishInRelative", "type": "Int", "unit": "seconds"},
            {"key": "bad key!", "type": "Boolean"},
        ]
    }
    state, units = plan(app)
    preheat = dynamic(units)["Fast preheat"]
    assert (preheat.type_name, preheat.has_value) == ("Switch", False)  # created, no value
    assert state.unit_kinds[HA][str(preheat.unit)] == "switch"
    assert "Finish in relative" not in dynamic(units)
    assert "bad key!" not in state.unit_alloc[HA]


def test_selectors_always_carry_their_custom_keys():
    app = oven_with_details()
    state, _ = plan(app)
    del app.options[SPIN]  # not offered now: the "Not available" path must keep them too
    for _ in range(2):
        _, units = plan(app, state)
        selectors = [u for n, u in units.items() if u.type_name == "Selector Switch" and n >= 20]
        assert selectors
        for sel in selectors:
            assert {"HCKey", "HCKind", "HCKeys", "LevelNames"} <= set(sel.options)
            assert sel.options["HCKeys"].count("|") == sel.options["LevelNames"].count("|")
        program = units[planner.UNIT_PROGRAM_SELECT].options
        assert program["HCKeys"].count("|") == program["LevelNames"].count("|")


def test_levels_are_append_only_and_kinds_stick():
    app = oven_with_details()
    state, units = plan(app)
    spin_unit = next(n for n, u in units.items() if u.name.endswith("Spin speed"))
    app.cache.program_details["P.A"]["options"][1]["constraints"]["allowedvalues"] = [
        "LaundryCare.Washer.EnumType.SpinSpeed.RPM1400",
        "LaundryCare.Washer.EnumType.SpinSpeed.RPM800",
    ]
    app.options[SPIN] = model.Value("LaundryCare.Washer.EnumType.SpinSpeed.RPM1400")
    _, units = plan(app, state)
    spin = units[spin_unit]
    assert spin.options["HCKeys"].split("|")[1:] == [
        "LaundryCare.Washer.EnumType.SpinSpeed.RPM800",
        "LaundryCare.Washer.EnumType.SpinSpeed.RPM1200",
        "LaundryCare.Washer.EnumType.SpinSpeed.RPM1400",
    ]
    assert spin.nvalue == 30


def test_option_not_in_current_program_shows_not_available():
    app = oven_with_details()
    state, units = plan(app)
    del app.options[SPIN]
    _, units = plan(app, state)
    spin = next(u for u in units.values() if u.name.endswith("Spin speed"))
    assert (spin.nvalue, spin.svalue) == (0, "0")


def test_options_wait_for_program_details():
    app = oven_with_details()
    app.cache.programs.append("P.B")  # details for P.B not fetched yet
    _, units = plan(app)
    assert not [n for n in units if n >= 20]


def test_settings_wait_for_constraints_and_then_get_their_kind():
    app = app_with()
    app.settings["BSH.Common.Setting.ChildLock"] = model.Value(False)
    _, units = plan(app)
    assert not [n for n in units if n >= 20]
    app.cache.setting_constraints["BSH.Common.Setting.ChildLock"] = {"access": "readWrite"}
    _, units = plan(app)
    child = next(u for n, u in units.items() if n >= 20)
    assert (child.type_name, child.nvalue, child.svalue) == ("Switch", 0, "Off")


def test_cavity_temperature_is_rounded_with_deadband():
    app = app_with(**{"Cooking.Oven.Status.CurrentCavityTemperature": 22.999999999999996})
    _, units = plan(app)
    temp = next(u for n, u in units.items() if n >= 20)
    assert (temp.type_name, temp.svalue, temp.deadband) == ("Temperature", "23", 0.5)


def test_unknown_values_are_created_unused_and_every_message_used():
    # Scripts load only used devices, and a message is what a notification hangs on, so every
    # event is used whether the catalog names it or not; feedback prompts and the dryer's
    # cool-down are the exception.
    app = app_with(**{"X.Y.Status.Thing": 3, "Cooking.Oven.Status.CurrentCavityTemperature": 21.0})
    app.status["X.Y.Status.Named"] = model.Value("on")
    app.names["X.Y.Status.Named"] = "Named by Home Connect"
    present = model.EventState(model.PRESENT, "hint", "")
    for key in (
        "BSH.Common.Event.ProgramFinished",
        "X.Y.Event.Odd",
        "LaundryCare.Dryer.Event.ProgramFeedback.CupboardDry.Request",
        "LaundryCare.Dryer.Event.CoolDownPhaseRunning",
    ):
        app.events[key] = present
    _, units = plan(app)
    used = {u.options["HCKey"]: u.used for n, u in units.items() if n >= 20}
    assert used == {
        "X.Y.Status.Thing": 0,
        "X.Y.Status.Named": 0,
        "Cooking.Oven.Status.CurrentCavityTemperature": 1,
        "BSH.Common.Event.ProgramFinished": 1,
        "X.Y.Event.Odd": 1,
        "LaundryCare.Dryer.Event.ProgramFeedback.CupboardDry.Request": 0,
        "LaundryCare.Dryer.Event.CoolDownPhaseRunning": 0,
    }
    assert all(u.used == 1 for n, u in units.items() if n < 20)


def test_event_alerts_and_favourite_triggers():
    app = app_with()
    app.events["BSH.Common.Event.ProgramFinished"] = model.EventState(
        model.PRESENT, "hint", "acknowledge"
    )
    app.events["Cooking.Hob.Event.Favorite.001"] = model.EventState(model.PRESENT, "hint", "none")
    _, units = plan(app)
    dyn = dynamic(units)
    finished = dyn["Program finished"]
    assert (finished.type_name, finished.nvalue) == ("Alert", catalog.YELLOW)
    fav = dyn["Favorite.001"]
    assert (fav.type_name, fav.switchtype, fav.nvalue) == ("Push On", 9, 1)
    app.events["BSH.Common.Event.ProgramFinished"] = model.EventState(
        "BSH.Common.EnumType.EventPresentState.Off", "hint", "acknowledge"
    )
    _, units = plan(app)
    finished = next(u for u in units.values() if u.name.endswith("Program finished"))
    assert finished.nvalue == catalog.GREY
    assert finished.svalue == "No notification"
    _, units = plan(app, lang="nl")
    cleared = next(u for u in units.values() if u.name.endswith("Programma klaar"))
    assert cleared.svalue == "Geen melding"


def test_hostile_names_are_escaped_and_bounded():
    app = app_with(**{catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.Run"})
    app.active_program = "P.X"
    app.names["P.X"] = '<img src=x onerror="alert(1)">'
    _, units = plan(app)
    assert "<img" not in units[planner.UNIT_STATE].svalue
    assert "&lt;img" in units[planner.UNIT_STATE].svalue
    long_label = "x" * 200
    updates = planner.plan_appliance(app, persistence.State(), "en", NOW, False, long_label)
    assert all(len(u.name) <= 100 for u in updates)


def test_hostile_appliance_name_gives_safe_bounded_device_names():
    m = model.Model()
    hostile = "<script>alert(1)</script>|Level;‮evil\n" + "y" * 300
    payload = {
        "data": {
            "homeappliances": [
                {"haId": HA, "name": hostile, "brand": "SIEMENS", "type": "Oven", "connected": True}
            ]
        }
    }
    m.apply_list(payload, m.tick())
    app = m.appliances[HA]
    app.status[catalog.OPERATION_STATE] = model.Value("BSH.Common.EnumType.OperationState.Run")
    app.status["X.Y.Status.Thing"] = model.Value('"><b>x</b>')
    label = planner.display_names([app])[HA]
    updates = planner.plan_appliance(app, persistence.State(), "en", NOW, False, label)
    assert updates
    for u in updates:
        assert len(u.name) <= 100
        assert not any(c in u.name for c in "<>|;\n‮")
        assert "<" not in u.svalue and ">" not in u.svalue


def test_offline_or_powered_off_appliance_still_gets_its_devices():
    app = oven_with_details()
    app.connected = False
    app.options.clear()
    app.settings[catalog.POWER_STATE] = model.Value("BSH.Common.EnumType.PowerState.Off")
    _, units = plan(app)
    assert units[planner.UNIT_STATE].svalue == "Offline"
    assert units[planner.UNIT_POWER].nvalue == 0
    dyn = dynamic(units)
    assert {"Set temperature", "Spin speed"} <= set(dyn)
    assert (dyn["Spin speed"].nvalue, dyn["Spin speed"].has_value) == (0, True)
    setpoint = dyn["Set temperature"]
    assert (setpoint.has_value, setpoint.options["ValueMax"]) == (False, "250")


def test_unit_exhaustion_degrades_without_crash():
    app = app_with()
    state = persistence.State(next_unit={HA: 255})
    for i in range(5):
        app.status[f"X.Y.Status.Thing{i}"] = model.Value(i)
    warnings: list[str] = []
    updates = planner.plan_appliance(app, state, "en", NOW, False, "Oven", warnings)
    assert all(u.unit <= planner.MAX_UNIT for u in updates)
    assert len([u for u in updates if u.unit >= 20]) == 1
    assert warnings == [
        f'unit_exhausted appliance="Oven" key="X.Y.Status.Thing{i}"' for i in range(1, 5)
    ]
    assert not any(HA in w for w in warnings)


def test_device_ids_are_the_readable_ha_id():
    assert planner.device_id(HA) == HA


def test_display_names_disambiguate_duplicates():
    a = model.Appliance("A1", name="Oven", brand="SIEMENS", type="Oven")
    b = model.Appliance("B1", name="Oven", brand="NEFF", type="Oven")
    c = model.Appliance("C1", name="Dryer", brand="BOSCH", type="Dryer")
    names = planner.display_names([a, b, c])
    assert names["C1"] == "Dryer"
    assert names["A1"] != names["B1"] and "SIEMENS" in names["A1"]


def test_display_names_warn_once_per_duplicate_and_stay_distinct():
    a = model.Appliance("A1", name="Oven", brand="SIEMENS", type="Oven")
    b = model.Appliance("B1", name="Oven", brand="SIEMENS", type="Oven")
    c = model.Appliance("C1", name="Dryer", brand="BOSCH", type="Dryer")
    warnings: list[str] = []
    names = planner.display_names([b, a, c], warnings)
    assert len({names["A1"], names["B1"], names["C1"]}) == 3
    assert names == planner.display_names([a, c, b])  # stable whatever the listing order
    assert len(warnings) == 1
    assert '"Oven"' in warnings[0] and "A1" not in warnings[0] and "B1" not in warnings[0]
    quiet: list[str] = []
    planner.display_names([c], quiet)
    assert quiet == []


def test_account_devices():
    view = planner.AccountView(
        "AWAITING_USER",
        user_code="ABCD-EFGH",
        link="https://api.home-connect.com/security/oauth/device_verify?user_code=ABCD-EFGH",
        valid_until="09:45",
        calls=17,
    )
    units = by_unit(planner.plan_account(view, "en"), planner.ACCOUNT_DEVICE)
    assert "ABCD-EFGH" in units[planner.ACCOUNT_TEXT].svalue
    assert "09:45" in units[planner.ACCOUNT_TEXT].svalue
    actions = units[planner.ACCOUNT_ACTIONS]
    assert actions.options["LevelNames"] == "-|Sign in again|Refresh appliances"
    assert (actions.nvalue, actions.svalue) == (0, "0")
    assert (units[planner.ACCOUNT_CALLS].svalue, units[planner.ACCOUNT_CALLS].used) == ("17", 0)
    signed = by_unit(
        planner.plan_account(planner.AccountView("SIGNED_OUT"), "nl"), planner.ACCOUNT_DEVICE
    )
    assert "Afgemeld" in signed[planner.ACCOUNT_TEXT].svalue


def test_dynamic_numbers_text_and_read_only_settings():
    app = app_with(**{"X.Y.Status.Flag": True})
    app.status["X.Y.Status.Delay"] = model.Value(61, "seconds")
    app.status["X.Y.Status.Volume"] = model.Value(2.5, "l")
    app.settings["X.Y.Setting.Level"] = model.Value(3)
    app.cache.setting_constraints["X.Y.Setting.Level"] = {"access": "read"}
    _, units = plan(app)
    dyn = dynamic(units)
    assert dyn["Flag"].svalue == "Yes"
    assert (dyn["Delay"].svalue, dyn["Delay"].options["Custom"]) == ("2", "1;min")
    assert (dyn["Volume"].svalue, dyn["Volume"].options["Custom"]) == ("2.5", "1;l")
    assert (dyn["Level"].type_name, dyn["Level"].svalue) == ("Custom", "3")


def test_remote_control_off_and_on():
    app = app_with(**{catalog.REMOTE_ACTIVE: False})
    _, units = plan(app)
    assert units[planner.UNIT_REMOTE].svalue == "Remote control off"
    app.status[catalog.REMOTE_ACTIVE] = model.Value(True)
    _, units = plan(app)
    assert (units[planner.UNIT_REMOTE].nvalue, units[planner.UNIT_REMOTE].svalue) == (
        catalog.GREY,
        "Remote control on",
    )


def test_events_degrade_when_units_run_out():
    app = app_with()
    app.events["BSH.Common.Event.ProgramFinished"] = model.EventState(model.PRESENT, "hint", "")
    state = persistence.State(next_unit={HA: planner.MAX_UNIT + 1})
    _, units = plan(app, state)
    assert not [n for n in units if n >= 20]


def test_account_message_is_escaped():
    view = planner.AccountView("NEEDS_REAUTH", message="<b>x</b>")
    text_unit = by_unit(planner.plan_account(view, "en"), planner.ACCOUNT_DEVICE)[1]
    assert text_unit.svalue.endswith("<ul><li>&lt;b&gt;x&lt;/b&gt;</li></ul>")


BAD_NUMBERS = [float("nan"), float("inf"), float("-inf"), 1e400, 10**400, 1e300]


def test_non_finite_and_huge_numbers_are_ignored_without_crash():
    for bad in BAD_NUMBERS:
        app = oven_with_details()
        app.active_program = "P.A"
        app.status[catalog.OPERATION_STATE] = model.Value("BSH.Common.EnumType.OperationState.Run")
        app.status["Cooking.Oven.Status.CurrentCavityTemperature"] = model.Value(bad, "°C")
        app.status["X.Y.Status.Count"] = model.Value(bad)
        app.options[catalog.PROGRESS] = model.Value(bad, "%")
        app.options[catalog.REMAINING] = model.Value(bad, "seconds")
        app.options[SETPOINT] = model.Value(bad, "°C")
        app.cache.program_details["P.A"]["options"][0]["constraints"]["max"] = bad
        _, units = plan(app)
        assert planner.UNIT_PROGRESS not in units and planner.UNIT_TIME not in units
        dyn = dynamic(units)
        assert "ValueMax" not in dyn["Set temperature"].options
        for name in ("Set temperature", "Cavity temperature", "Count"):
            assert dyn[name].has_value is False, (bad, name)


def test_unknown_operation_state_is_escaped_and_bounded():
    hostile = "BSH.Common.EnumType.OperationState." + "<script>alert(1)</script>" * 20
    app = app_with(**{catalog.OPERATION_STATE: hostile})
    _, units = plan(app)
    svalue = units[planner.UNIT_STATE].svalue
    assert "<" not in svalue and ">" not in svalue
    assert len(svalue) <= 60


def test_hostile_units_are_bounded_and_cleaned():
    app = oven_with_details()
    app.options[SETPOINT] = model.Value(180.0, "<b>°C;|</b>" + "x" * 100)
    app.status["X.Y.Status.Volume"] = model.Value(2.0, "<i>l;</i>" + "y" * 100)
    _, units = plan(app)
    dyn = dynamic(units)
    unit_text = dyn["Set temperature"].options["ValueUnit"]
    custom = dyn["Volume"].options["Custom"].removeprefix("1;")
    for shown in (unit_text, custom):
        assert len(shown) <= 20 and not any(c in shown for c in "<>;|")


def test_fridge_compartment_doors_are_known_contacts_and_settings_are_not_doors():
    app = app_with(**{"Refrigeration.Common.Status.Door.Freezer": "X.DoorState.Closed"})
    setting = "Refrigeration.Common.Setting.Door.AssistantTriggerFreezer"
    app.settings[setting] = model.Value("X.AssistantTrigger.Push")
    # Read-only, so the read-only kind rules (where the door pattern lives) decide its kind.
    app.cache.setting_constraints[setting] = {"access": "read"}
    _, units = plan(app, lang="nl")
    dyn = dynamic(units)
    door = dyn["Deur vriezer"]
    assert (door.switchtype, door.nvalue, door.used) == (11, 0, 1)
    assistant = next(u for u in dyn.values() if u.options["HCKey"] == setting)
    assert (assistant.type_name, assistant.switchtype) == ("Text", 0)


def test_first_discovery_offline_with_empty_cache():
    app = model.Appliance(HA, name="Oven 1", brand="SIEMENS", type="Oven", connected=False)
    state, units = plan(app)
    assert units[planner.UNIT_STATE].svalue == "Offline"
    created = {n for n, u in units.items() if u.create}
    assert created == {planner.UNIT_STATE, planner.UNIT_STATUS_CODE}
    assert state.unit_alloc.get(HA, {}) == {}


def delayed(**options):
    app = app_with(**{catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.DelayedStart"})
    app.active_program = "P.A"
    app.names["P.A"] = "Eco"
    for key, seconds in options.items():
        app.options[key] = model.Value(seconds, "seconds")
    return app


def test_delayed_start_finish_time_laundry_uses_finish_in():
    app = delayed(**{catalog.FINISH_IN: 7200, catalog.REMAINING: 3600})
    _, units = plan(app)
    expected = planner.clock(round((NOW + 7200) / 60) * 60)
    assert units[planner.UNIT_PROGRAM].svalue == f"Eco, ready at {expected}"


def test_delayed_start_finish_time_oven_and_dishwasher_add_start_in():
    app = delayed(**{catalog.START_IN: 1800, catalog.REMAINING: 3600})
    app.options[catalog.ESTIMATED] = model.Value(True)
    _, units = plan(app)
    expected = planner.clock(round((NOW + 5400) / 60) * 60)
    assert units[planner.UNIT_PROGRAM].svalue == f"Eco, ready at about {expected}"
    del app.options[catalog.REMAINING]  # no duration known: no finish time is invented
    _, units = plan(app)
    assert units[planner.UNIT_PROGRAM].svalue == "Eco"


def test_finish_text_is_stable_within_a_minute():
    app = app_with(**{catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.Run"})
    app.active_program = "P.A"
    app.names["P.A"] = "Eco"
    app.options[catalog.REMAINING] = model.Value(3600, "seconds")
    first = by_unit(planner.plan_appliance(app, persistence.State(), "en", NOW, False, "x"))
    app.options[catalog.REMAINING] = model.Value(3590, "seconds")
    later = by_unit(planner.plan_appliance(app, persistence.State(), "en", NOW + 10, False, "x"))
    assert first[planner.UNIT_PROGRAM].svalue == later[planner.UNIT_PROGRAM].svalue


def counting_down(state="Run", **countdowns):
    app = app_with(**{catalog.OPERATION_STATE: f"BSH.Common.EnumType.OperationState.{state}"})
    app.active_program = "P.A"
    app.names["P.A"] = "Eco"
    for key, seconds in countdowns.items():
        app.options[key] = model.Value(seconds, "seconds")
        app.received[key] = NOW
    return app


def program_text(app, now):
    updates = planner.plan_appliance(app, persistence.State(), "en", now, False, "x")
    return by_unit(updates)[planner.UNIT_PROGRAM].svalue


def test_finish_time_does_not_drift_between_events():
    for app in (
        counting_down(**{catalog.REMAINING: 3600}),
        counting_down("DelayedStart", **{catalog.FINISH_IN: 7200}),
        counting_down("DelayedStart", **{catalog.START_IN: 1800, catalog.REMAINING: 3600}),
    ):
        first = program_text(app, NOW)
        assert first == program_text(app, NOW + 600)


def test_finish_time_is_anchored_to_the_receive_time():
    app = counting_down(**{catalog.REMAINING: 3600})
    app.received[catalog.REMAINING] = NOW - 600  # received ten minutes ago
    expected = planner.clock(round((NOW + 3000) / 60) * 60)
    assert program_text(app, NOW) == f"Eco, ready at {expected}"
    oven = counting_down("DelayedStart", **{catalog.START_IN: 1800, catalog.REMAINING: 3600})
    oven.received[catalog.START_IN] = NOW - 600
    expected = planner.clock(round((NOW + 4800) / 60) * 60)
    assert program_text(oven, NOW) == f"Eco, ready at {expected}"


def test_consistent_countdown_event_keeps_the_minute():
    app = counting_down(**{catalog.REMAINING: 3600})
    first = program_text(app, NOW)
    app.options[catalog.REMAINING] = model.Value(3060, "seconds")
    app.received[catalog.REMAINING] = NOW + 540
    assert program_text(app, NOW + 600) == first


def test_program_change_rewrites_the_finish_text():
    app = counting_down(**{catalog.REMAINING: 3600})
    first = program_text(app, NOW)
    app.active_program = "P.B"
    app.names["P.B"] = "Cotton"
    assert program_text(app, NOW) == first.replace("Eco", "Cotton")
    assert program_text(app, NOW) != first


def test_finish_time_through_the_model_uses_the_event_time():
    clock = [NOW]
    m = model.Model(wall=lambda: clock[0])
    listing = {"data": {"homeappliances": [{"haId": HA, "name": "Oven", "connected": True}]}}
    m.apply_list(listing, m.tick())
    items = [
        {"key": catalog.OPERATION_STATE, "value": "BSH.Common.EnumType.OperationState.Run"},
        {"key": "BSH.Common.Root.ActiveProgram", "value": "P.A"},
        {"key": catalog.REMAINING, "value": 3600, "unit": "seconds"},
    ]
    m.apply_event(sse.Event("NOTIFY", json.dumps({"haId": HA, "items": items}), HA))
    app = m.appliances[HA]
    first = program_text(app, NOW)
    assert first == program_text(app, NOW + 600)


CAVITY = "Cooking.Oven.Status.CurrentCavityTemperature"


def _names(units) -> list[str]:
    return [u.name for u in units.values()]


def test_catalog_names_keep_alike_reported_names_apart():
    app = oven_with_details()
    app.status[CAVITY] = model.Value(23.0, "°C")
    app.names[CAVITY] = "Cavity temperature"
    app.names[SETPOINT] = "Cavity temperature"  # what Home Connect really calls the option
    _, units = plan(app)
    dyn = dynamic(units)
    assert dyn["Cavity temperature"].options["HCKey"] == CAVITY
    assert dyn["Set temperature"].options["HCKey"] == SETPOINT
    assert len(set(_names(units))) == len(units)


def test_reported_name_never_replaces_a_catalog_name():
    app = oven_with_details()
    app.names[SETPOINT] = "Cavity temperature"
    _, units = plan(app)
    assert dynamic(units)["Set temperature"].options["HCKey"] == SETPOINT


def test_other_collisions_get_a_short_disambiguator():
    app = app_with()
    # Two keys the catalog does not name, reported under the same name, plus one that takes a
    # fixed unit's name.
    app.status["Vendor.Status.A"] = model.Value("x")
    app.status["Vendor.Status.B"] = model.Value("y")
    app.status["Vendor.Status.C"] = model.Value("z")
    app.names["Vendor.Status.A"] = "Mode"
    app.names["Vendor.Status.B"] = "Mode"
    app.names["Vendor.Status.C"] = "State"
    app.status[catalog.OPERATION_STATE] = model.Value("BSH.Common.EnumType.OperationState.Ready")
    state, units = plan(app)
    names = _names(units)
    assert len(set(names)) == len(names)
    assert units[planner.UNIT_STATE].name == "Oven 1 State"  # a fixed unit keeps its name
    by_key = {u.options.get("HCKey"): u.name for u in units.values()}
    assert by_key["Vendor.Status.A"] == "Oven 1 Mode"  # the lowest unit keeps the name
    assert by_key["Vendor.Status.B"] == "Oven 1 Mode (2)"
    assert by_key["Vendor.Status.C"] == "Oven 1 State (2)"
    # Stable across passes: the same plan gives the same names.
    _, again = plan(app, state)
    assert _names(again) == names


def test_disambiguated_name_respects_the_length_cap():
    app = app_with()
    long_name = "x" * 200
    for key in ("Vendor.Status.A", "Vendor.Status.B"):
        app.status[key] = model.Value("v")
        app.names[key] = long_name
    _, units = plan(app)
    names = _names(units)
    assert len(set(names)) == len(names)
    assert all(len(n) <= 100 for n in names)


def test_renaming_for_uniqueness_never_touches_a_user_name(domoticz):
    app = oven_with_details()
    app.status[CAVITY] = model.Value(23.0, "°C")
    app.names[CAVITY] = "Cavity temperature"
    state = persistence.State()
    domoticz_api.apply_updates(domoticz.Devices, plan(app, state)[1].values(), state)
    units = domoticz.Devices[HA].Units
    setpoint_unit = state.unit_alloc[HA][SETPOINT]
    assert units[setpoint_unit].Name == "Oven 1 Set temperature"
    units[setpoint_unit].Name = "My setpoint"  # the user's own name
    app.names[SETPOINT] = "Cavity temperature"
    domoticz_api.apply_updates(domoticz.Devices, plan(app, state)[1].values(), state)
    assert units[setpoint_unit].Name == "My setpoint"


VERIFY = "https://api.home-connect.com/security/oauth/device_verify?user_code=ABCD-EFGH&x=1"
ESCAPED = VERIFY.replace("&", "&amp;")
LABEL = {"en": "Sign in to Home Connect", "nl": "Aanmelden bij Home Connect"}
REAUTH_STATES = ("AUTHORIZED", "NEEDS_REAUTH", "NEEDS_USER_ACTION")


def pending_text(state, link, lang="en", message="", code="ABCD-EFGH"):
    view = planner.AccountView(
        state, user_code=code, link=link, valid_until="09:45", message=message
    )
    return by_unit(planner.plan_account(view, lang), planner.ACCOUNT_DEVICE)[
        planner.ACCOUNT_TEXT
    ].svalue


def items_of(svalue):
    assert svalue.count("<ul>") == 1 and svalue.endswith("</ul>")
    return (
        svalue.split("<ul>", 1)[1]
        .removesuffix("</li></ul>")
        .removeprefix("<li>")
        .split("</li><li>")
    )


def test_link_markup_uses_a_short_label():
    for lang in ("en", "nl"):
        anchor = planner.link_markup(VERIFY, lang)
        assert anchor == f'<a href="{ESCAPED}" target="_blank">{LABEL[lang]}</a>'
        assert planner.link_markup("", lang) == ""


CODE_ITEM = {"en": "Code ABCD-EFGH (until 09:45)", "nl": "Code ABCD-EFGH (tot 09:45)"}


def test_awaiting_user_is_a_short_line_and_a_link_and_code_list():
    head = {"en": "Sign-in needed", "nl": "Aanmelden nodig"}
    for lang in ("en", "nl"):
        svalue = pending_text("AWAITING_USER", VERIFY, lang)
        assert svalue.split("<ul>", 1)[0] == head[lang]
        assert items_of(svalue) == [planner.link_markup(VERIFY, lang), CODE_ITEM[lang]]
        assert svalue.count("<a ") == 1 and VERIFY.split("?")[0] + "<" not in svalue
        assert "\n" not in svalue and "> <" not in svalue


def test_reauthorize_states_use_a_short_line_and_the_same_two_bullets():
    heads = {
        ("AUTHORIZED", "en"): "Signed in",
        ("AUTHORIZED", "nl"): "Aangemeld",
        ("NEEDS_REAUTH", "en"): "Sign-in lost",
        ("NEEDS_REAUTH", "nl"): "Aanmelding verloren",
        ("NEEDS_USER_ACTION", "en"): "Action needed: accept terms",
        ("NEEDS_USER_ACTION", "nl"): "Actie nodig: accept terms",
    }
    again = {"en": "Again: ", "nl": "Opnieuw: "}
    for state in REAUTH_STATES:
        for lang in ("en", "nl"):
            message = "accept terms" if state == "NEEDS_USER_ACTION" else ""
            svalue = pending_text(state, VERIFY, lang, message=message)
            assert svalue.split("<ul>", 1)[0] == heads[(state, lang)]
            assert items_of(svalue) == [
                again[lang] + planner.link_markup(VERIFY, lang),
                CODE_ITEM[lang],
            ]


def test_single_line_states_are_short():
    expected = {
        ("NEEDS_REAUTH", "en"): 'Sign-in lost: choose "Sign in again"',
        ("NEEDS_REAUTH", "nl"): 'Aanmelding verloren: kies "Opnieuw aanmelden"',
        ("SIGNED_OUT", "en"): "Signed out (turn off Sign out to sign in)",
        ("SIGNED_OUT", "nl"): "Afgemeld (zet Sign out uit om je aan te melden)",
        ("EXPIRED_CODE", "en"): 'Code expired: choose "Sign in again"',
        ("EXPIRED_CODE", "nl"): 'Code verlopen: kies "Opnieuw aanmelden"',
        ("AUTHORIZED", "en"): "Signed in",
        ("UNAUTHORIZED", "nl"): "Aanmelden wordt gestart",
    }
    for (state, lang), line in expected.items():
        assert pending_text(state, "", lang, code="") == line


def test_user_action_without_a_pending_link_hints_re_authorize():
    svalue = pending_text("NEEDS_USER_ACTION", "", "nl", message="voorwaarden", code="")
    assert svalue == 'Actie nodig: voorwaarden<ul><li>Kies "Opnieuw aanmelden"</li></ul>'


def test_notes_become_their_own_escaped_bullet():
    items = items_of(pending_text("AUTHORIZED", VERIFY, message="Failed (x <b>)"))
    assert items[-1] == "Failed (x &lt;b&gt;)"
    action = pending_text("NEEDS_USER_ACTION", VERIFY, message="terms <b>")
    assert "terms &lt;b&gt;" in action.split("<ul>", 1)[0]  # in the line, not repeated
    assert "terms" not in "".join(items_of(action))
    for state in ("AUTHORIZED", "NEEDS_REAUTH", "SIGNED_OUT", "EXPIRED_CODE"):
        svalue = pending_text(state, "", message="note <i>", code="")
        assert "<a" not in svalue and "<i>" not in svalue
        assert svalue.endswith("<ul><li>note &lt;i&gt;</li></ul>")


def test_hostile_values_stay_escaped_inside_items():
    hostile = 'https://api.home-connect.com/x"><script>alert(1)</script>'
    svalue = pending_text("AWAITING_USER", hostile, code="<b>X</b>", message="<img src=x>")
    assert "<script>" not in svalue and "<b>" not in svalue and "<img" not in svalue
    assert '"><script>' not in svalue
    tags = svalue.count("<")
    assert tags == svalue.count("<ul>") + svalue.count("</ul>") + svalue.count("<li>") + (
        svalue.count("</li>") + svalue.count("<a ") + svalue.count("</a>")
    )


def test_empty_link_gives_no_anchor():
    for lang in ("en", "nl"):
        for state in ("AWAITING_USER", *REAUTH_STATES):
            assert "<a" not in pending_text(state, "", lang)


def test_bullets_skips_empty_items_and_empty_lists():
    assert planner.bullets(["a", "", "b"]) == "<ul><li>a</li><li>b</li></ul>"
    assert planner.bullets(["", ""]) == ""


def step_of(option, value):
    app = oven_with_details()
    app.cache.program_details["P.A"]["options"].append(option)
    app.options[option["key"]] = value
    _, units = plan(app)
    return next(u for u in units.values() if u.options.get("HCKey") == option["key"])


def test_setpoint_without_stepsize_steps_by_one_and_shows_seconds_as_minutes():
    temp = step_of(
        {
            "key": "X.Y.Option.Heat",
            "type": "Double",
            "unit": "°C",
            "constraints": {"min": 30, "max": 275},
        },
        model.Value(180.0, "°C"),
    )
    assert temp.options["ValueStep"] == "1"
    duration = step_of(
        {
            "key": "BSH.Common.Option.Duration",
            "type": "Int",
            "unit": "seconds",
            "constraints": {"min": 1, "max": 5400},
        },
        model.Value(600, "seconds"),
    )
    assert duration.options["ValueStep"] == "1"
    assert duration.options["ValueUnit"] == "min"
    # 1 s rounds up to 0.02 min, so the card's own minimum is a value the plugin accepts.
    assert (duration.options["ValueMin"], duration.options["ValueMax"]) == ("0.02", "90")
    assert duration.svalue == "10.0"
    odd = step_of(
        {
            "key": "BSH.Common.Option.Duration",
            "type": "Int",
            "unit": "seconds",
            "constraints": {"min": 59, "max": 5399},
        },
        model.Value(80, "seconds"),
    )
    assert (odd.options["ValueMin"], odd.options["ValueMax"], odd.svalue) == (
        "0.99",
        "89.98",
        "1.33",
    )
    stepped = step_of(
        {
            "key": "BSH.Common.Option.Duration",
            "type": "Int",
            "unit": "seconds",
            "constraints": {"min": 60, "max": 5400, "stepsize": 30},
        },
        model.Value(90, "seconds"),
    )
    assert (stepped.options["ValueStep"], stepped.svalue) == ("0.5", "1.5")
    given = step_of(
        {
            "key": "X.Y.Option.Heat",
            "type": "Double",
            "unit": "°C",
            "constraints": {"min": 30, "max": 275, "stepsize": 5},
        },
        model.Value(180.0, "°C"),
    )
    assert given.options["ValueStep"] == "5"


def test_unnamed_enum_value_is_humanized():
    app = app_with(**{"X.Y.Status.Refill": "X.Y.EnumType.Refill.PossibleOpenDoor"})
    app.status["X.Y.Status.Other"] = model.Value("X.Y.EnumType.Refill.Full")
    app.value_names["X.Y.EnumType.Refill.Full"] = "Vol"
    _, units = plan(app)
    dyn = dynamic(units)
    assert dyn["Refill"].svalue == "Possible open door"
    assert dyn["Other"].svalue == "Vol"


def appliance_of(kind):
    return model.Appliance(HA, name="Thing", brand="BOSCH", type=kind, connected=True)


def test_icons_follow_the_appliance_type_for_its_switches():
    washer = appliance_of("Washer")
    washer.settings[catalog.POWER_STATE] = model.Value("BSH.Common.EnumType.PowerState.On")
    washer.status[catalog.OPERATION_STATE] = model.Value("BSH.Common.EnumType.OperationState.Run")
    washer.status[catalog.REMOTE_ACTIVE] = model.Value(True)
    washer.active_program = "P.A"
    washer.options[catalog.REMAINING] = model.Value(600, "seconds")
    washer.options[catalog.PROGRESS] = model.Value(10, "%")
    _, units = plan(washer)
    assert units[planner.UNIT_POWER].image == catalog.IMAGE_WASHING_MACHINE == 28
    assert units[planner.UNIT_TIME].image == catalog.IMAGE_CLOCK == 21  # Custom honours it
    for unit in (
        planner.UNIT_STATE,
        planner.UNIT_PROGRAM,
        planner.UNIT_REMOTE,
        planner.UNIT_PROGRESS,
    ):
        assert units[unit].image == 0  # Alert, Text, Percentage draw their own icon


def test_oven_option_switch_and_selector_get_the_oven_icon():
    app = oven_with_details()
    app.cache.program_details["P.A"]["options"].append(
        {"key": "Cooking.Oven.Option.FastPreHeat", "type": "Boolean"}
    )
    app.options["Cooking.Oven.Option.FastPreHeat"] = model.Value(True)
    app.status[catalog.DOOR_STATE] = model.Value("BSH.Common.EnumType.DoorState.Closed")
    app.events["BSH.Common.Event.ProgramFinished"] = model.EventState(model.PRESENT, "hint", "")
    _, units = plan(app)
    dyn = dynamic(units)
    assert dyn["Fast preheat"].image == catalog.IMAGE_HEATING == 15
    assert dyn["Spin speed"].image == 15
    assert dyn["Set temperature"].image == 0  # Set Point ignores CustomImage
    assert dyn["Program finished"].image == 0  # Alert
    assert units[planner.UNIT_DOOR].image == 0  # the door contact keeps its own rendering


def test_icon_per_appliance_type_and_unknown_type_has_none():
    expected = {
        "Dryer": 28,
        "WasherDryer": 28,
        "FridgeFreezer": 16,
        "Freezer": 16,
        "CoffeeMaker": 24,
        "Hood": 7,
        "Dishwasher": 11,
        "Microwave": 15,
        "Hob": 15,
        "Robot": 0,
    }
    for kind, image in expected.items():
        app = appliance_of(kind)
        app.settings[catalog.POWER_STATE] = model.Value("BSH.Common.EnumType.PowerState.On")
        _, units = plan(app)
        assert units[planner.UNIT_POWER].image == image, kind


def test_account_devices_have_no_icon():
    view = planner.AccountView("AUTHORIZED")
    assert all(u.image == 0 for u in planner.plan_account(view, "en"))


RUN_CARD_TEXT = {
    "en": {"finished": "600 watt, finished", "selected": "600 watt (selected)"},
    "nl": {"finished": "600 watt, klaar", "selected": "600 watt (gekozen)"},
}


def microwave(state_name, lang="en", active=True, received=NOW):
    app = app_with(**{catalog.OPERATION_STATE: f"BSH.Common.EnumType.OperationState.{state_name}"})
    app.names["P.MW"] = "600 watt"
    app.cache.programs = ["P.MW"]
    app.cache.program_details["P.MW"] = {}
    app.selected_program = "P.MW"
    app.active_program = "P.MW" if active else None
    app.options[catalog.REMAINING] = model.Value(60, "seconds")
    app.received[catalog.REMAINING] = received
    app.options[catalog.PROGRESS] = model.Value(90, "%")
    return app


def run_cards(app, lang, now=NOW):
    units = by_unit(planner.plan_appliance(app, persistence.State(), lang, now, False, "x"))
    return {n: units[n].svalue for n in (4, 5, 6) if n in units}


def test_run_cards_reset_when_the_program_ends():
    for lang in ("en", "nl"):
        running = run_cards(microwave("Run"), lang)
        assert running[4] == "90" and running[5] == "1"
        assert running[6].startswith("600 watt, ") and planner.clock(NOW + 60) in running[6]
        finished = run_cards(microwave("Finished"), lang, NOW + 300)
        assert finished == {4: "100", 5: "0", 6: RUN_CARD_TEXT[lang]["finished"]}
        ready = run_cards(microwave("Ready", active=False), lang, NOW + 600)
        assert ready == {4: "0", 5: "0", 6: RUN_CARD_TEXT[lang]["selected"]}
        # An active program left over in the model does not count once the state is idle.
        assert run_cards(microwave("Inactive"), lang, NOW + 600) == ready


def test_idle_without_a_selected_program_shows_a_dash():
    app = microwave("Ready", active=False)
    app.selected_program = None
    assert run_cards(app, "en")[6] == "-"


def test_offline_plans_nothing_for_the_run_cards():
    app = microwave("Run")
    app.connected = False
    assert run_cards(app, "nl") == {}


def card_6(app, now=NOW, lang="en"):
    units = by_unit(planner.plan_appliance(app, persistence.State(), lang, now, False, "x"))
    return (units[6].svalue, units[6].create) if 6 in units else None


def test_offline_run_drops_a_finish_time_once_it_has_passed():
    app = microwave("Run")
    app.connected = False
    assert card_6(app, NOW + 30) is None  # still ahead: the card stays as it was
    assert card_6(app, NOW + 7200) == ("600 watt", False)
    assert run_cards(app, "en", NOW + 7200) == {6: "600 watt"}


def test_offline_finished_run_keeps_its_card():
    app = microwave("Finished")
    app.connected = False
    assert card_6(app, NOW + 7200) is None


def test_restart_without_a_known_run_resets_the_program_card():
    # After a restart the model knows no run, so a stored "ready at" must not survive.
    for connected in (True, False):
        app = model.Appliance(HA, name="Oven 1", type="Oven", connected=connected)
        app.cache.programs = ["P.MW"]
        assert card_6(app, lang="nl") == ("-", False), connected
        app.selected_program = "P.MW"
        app.names["P.MW"] = "600 watt"
        assert card_6(app, lang="nl")[0] == "600 watt (gekozen)", connected
    fridge = model.Appliance(HA, name="Fridge", type="FridgeFreezer", connected=False)
    assert card_6(fridge) is None


def test_disconnect_keeps_a_running_washer_cards_and_shows_offline(domoticz):
    # Spec 7.4: DISCONNECTED shows Offline on unit 1 and writes nothing else.
    m = model.Model(wall=lambda: NOW)
    m.apply_list(
        {
            "data": {
                "homeappliances": [{"haId": HA, "name": "W", "type": "Washer", "connected": True}]
            }
        },
        m.tick(),
    )
    m.apply_status(
        HA,
        {
            "data": {
                "status": [
                    {
                        "key": catalog.OPERATION_STATE,
                        "value": "BSH.Common.EnumType.OperationState.Run",
                    }
                ]
            }
        },
        m.tick(),
    )
    options = [
        {"key": catalog.REMAINING, "value": 3600},
        {"key": catalog.PROGRESS, "value": 40},
    ]
    program = {"key": "LaundryCare.Washer.Program.Cotton", "name": "Cotton", "options": options}
    m.apply_program(HA, "active", {"data": program}, m.tick())
    state = persistence.State()
    app = m.appliances[HA]
    write = lambda: domoticz_api.apply_updates(  # noqa: E731
        domoticz.Devices, planner.plan_appliance(app, state, "en", NOW, False, "W"), state
    )
    write()
    units = domoticz.Devices[HA].Units
    before = {n: units[n].sValue for n in (4, 5, 6)}
    assert before[4] == "40" and before[5] == "60" and "ready at" in before[6]
    m.apply_event(sse.Event("DISCONNECTED", "", HA))
    write()
    assert {n: units[n].sValue for n in (4, 5, 6)} == before
    assert units[planner.UNIT_STATE].sValue == "Offline"


def test_finished_without_reported_progress_leaves_progress_alone():
    app = microwave("Finished")
    del app.options[catalog.PROGRESS]
    assert 4 not in run_cards(app, "en")


def test_cleared_active_program_resets_while_the_state_lags():
    app = microwave("Run", active=False)  # ActiveProgram cleared before the state event
    assert run_cards(app, "en") == {4: "0", 5: "0", 6: "600 watt (selected)"}


def test_never_a_finish_time_in_the_past():
    for lang in ("en", "nl"):
        app = microwave("Run", received=NOW - 600)  # the countdown ended ten minutes ago
        text_6 = run_cards(app, lang)[6]
        assert text_6 == "600 watt"
        assert "ready at" not in text_6 and "klaar om" not in text_6


def test_restart_resets_run_cards_that_exist_without_creating_them():
    # A fresh model after a restart: no progress or countdown reported, the appliance idle.
    app = app_with(**{catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.Inactive"})
    _, units = plan(app, lang="nl")
    resets = {n: (units[n].svalue, units[n].create) for n in (4, 5, 6)}
    assert resets == {4: ("0", False), 5: ("0", False), 6: ("-", False)}
    finished = app_with(**{catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.Finished"})
    _, units = plan(finished)
    assert 4 not in units  # finished without a reported progress leaves it alone
    assert (units[5].svalue, units[5].create) == ("0", False)


def test_reported_run_values_still_create_their_units():
    for state_name in ("Run", "Finished", "Ready"):
        units = by_unit(
            planner.plan_appliance(
                microwave(state_name), persistence.State(), "en", NOW, False, "x"
            )
        )
        assert all(units[n].create for n in (4, 5, 6)), state_name


def test_program_less_appliance_never_gains_run_cards(domoticz):
    fridge = model.Appliance(HA, name="Fridge", brand="BOSCH", type="FridgeFreezer", connected=True)
    fridge.status[catalog.OPERATION_STATE] = model.Value(
        "BSH.Common.EnumType.OperationState.Inactive"
    )
    state = persistence.State()
    updates = planner.plan_appliance(fridge, state, "en", NOW, False, "Fridge")
    domoticz_api.apply_updates(domoticz.Devices, updates, state)
    assert set(domoticz.Devices[HA].Units) == {planner.UNIT_STATE, planner.UNIT_STATUS_CODE}


def test_restart_resets_existing_run_cards_through_the_writer(domoticz):
    state = persistence.State()
    running = microwave("Run")
    domoticz_api.apply_updates(
        domoticz.Devices, planner.plan_appliance(running, state, "en", NOW, False, "x"), state
    )
    units = domoticz.Devices[HA].Units
    assert (units[4].sValue, units[5].sValue) == ("90", "1")
    fresh = app_with(**{catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.Inactive"})
    domoticz_api.apply_updates(
        domoticz.Devices, planner.plan_appliance(fresh, state, "en", NOW, False, "x"), state
    )
    assert (units[4].sValue, units[5].sValue, units[6].sValue) == ("0", "0", "-")


U1_NAMES = {
    "Cooking.Oven.Option.SetpointTemperature": ("Set temperature", "Ingestelde temperatuur"),
    "BSH.Common.Option.Duration": ("Duration", "Duur"),
    "LaundryCare.Washer.Setting.IDos1BaseLevel": ("i-DOS 1 base dose", "i-DOS 1 basisdosering"),
    "LaundryCare.Washer.Setting.IDos2BaseLevel": ("i-DOS 2 base dose", "i-DOS 2 basisdosering"),
    "Cooking.Oven.Option.FastPreHeat": ("Fast preheat", "Snel voorverwarmen"),
    "BSH.Common.Setting.AlarmClock": ("Timer", "Timer"),
    "BSH.Common.Setting.ChildLock": ("Child lock", "Kinderslot"),
    "Cooking.Common.Setting.Lighting": ("Light", "Verlichting"),
    "Cooking.Common.Setting.LightingBrightness": ("Light brightness", "Helderheid verlichting"),
    "BSH.Common.Setting.AmbientLightEnabled": ("Ambient light", "Sfeerverlichting"),
    "BSH.Common.Setting.AmbientLightBrightness": ("Ambient brightness", "Helderheid sfeerlicht"),
    "BSH.Common.Setting.AmbientLightColor": ("Ambient colour", "Kleur sfeerlicht"),
}


def test_catalog_names_win_over_home_connect_names():
    for key, (en, nl) in U1_NAMES.items():
        hc_names = {key: "Awkward wording from Home Connect"}
        assert catalog.name_for(hc_names, key, "en") == en
        assert catalog.name_for(hc_names, key, "nl") == nl
    # Home Connect's name only for a key the catalog does not know.
    assert catalog.name_for({"X.Y.Status.Odd": "Odd thing"}, "X.Y.Status.Odd", "en") == "Odd thing"


def test_auto_named_unit_is_renamed_to_the_catalog_name_once(domoticz):
    key = "BSH.Common.Setting.ChildLock"
    app = app_with()
    app.settings[key] = model.Value(False)
    app.names[key] = "Childproof lock"  # as Home Connect names it
    app.cache.setting_constraints[key] = {"access": "readWrite"}
    state = persistence.State()
    unit_no = 20
    # A unit created by an earlier version under Home Connect's wording.
    stale = planner.plan_appliance(app, state, "en", NOW, False, app.name)
    stale = [replace(u, name="Oven 1 Childproof lock") if u.unit == unit_no else u for u in stale]
    domoticz_api.apply_updates(domoticz.Devices, stale, state)
    unit = domoticz.Devices[HA].Units[unit_no]
    assert unit.Name == "Oven 1 Childproof lock"
    write = lambda: domoticz_api.apply_updates(  # noqa: E731
        domoticz.Devices, planner.plan_appliance(app, state, "en", NOW, False, app.name), state
    )
    write()
    assert unit.Name == "Oven 1 Child lock"
    unit.Name = "My own name"  # a user's rename is never undone
    write()
    assert unit.Name == "My own name"


def test_internal_and_structured_values_get_no_device():
    app = app_with()
    history = "LaundryCare.Washer.Status.Program.History."
    for leaf, value in (("EffectiveTime", "XXXXXXXX"), ("Uid", "YYYYYYYY"), ("Program08", "ZZZZ")):
        app.status[history + leaf] = model.Value(value)
    app.status["LaundryCare.Washer.Status.Program.Details.Phase"] = model.Value("x")
    app.status["BSH.Common.Status.ProgramSessionSummary.Latest"] = model.Value(
        {"energy": 830, "start": "2026-10-15T08:00:00Z"}
    )
    app.status["X.Y.Status.ListValue"] = model.Value([1, 2])
    app.settings["X.Y.Setting.DictValue"] = model.Value({"a": 1})
    app.cache.setting_constraints["X.Y.Setting.DictValue"] = {}
    app.options["X.Y.Option.ListValue"] = model.Value([1])
    app.status["X.Y.Status.Plain"] = model.Value("kept")
    state, units = plan(app)
    keys = {u.options.get("HCKey") for n, u in units.items() if n >= 20}
    assert keys == {"X.Y.Status.Plain"}
    assert set(state.unit_alloc[HA]) == {"X.Y.Status.Plain"}


def test_option_unit_of_a_structured_value_is_left_alone_after_a_restart():
    # Created as text by an earlier version, for a value no device can show; after a restart
    # the model holds no value for it and no cached program offers it.
    packed = "X.Y.Option.Packed"
    state = persistence.State(
        unit_alloc={HA: {packed: 30}}, unit_kinds={HA: {"30": "text"}}, next_unit={HA: 31}
    )
    app = oven_with_details()
    _, units = plan(app, state)
    assert 30 not in units
    # While a value is known in this run, a structured one is skipped as well.
    app.options[packed] = model.Value({"a": 1})
    _, units = plan(app, state)
    assert 30 not in units


STATUS_CODES = {
    "Inactive": 10,
    "Ready": 20,
    "DelayedStart": 30,
    "Run": 40,
    "Pause": 50,
    "ActionRequired": 60,
    "Finished": 70,
    "Aborting": 80,
    "Error": 90,
}


def status_code(app, lang="en"):
    units = by_unit(planner.plan_appliance(app, persistence.State(), lang, NOW, False, "Oven 1"))
    return units.get(planner.UNIT_STATUS_CODE)


def test_status_code_selector_maps_each_state_to_its_fixed_level():
    for state_name, level in STATUS_CODES.items():
        app = app_with(
            **{catalog.OPERATION_STATE: f"BSH.Common.EnumType.OperationState.{state_name}"}
        )
        unit = status_code(app)
        assert (unit.nvalue, unit.svalue) == (level, str(level)), state_name
    unit = status_code(
        app_with(**{catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.Run"})
    )
    # Used, so dzVents and Lua load it; the leading "$" keeps it off the dashboard.
    assert unit.name == "$Oven 1 Status code"
    assert (unit.type_name, unit.switchtype, unit.used) == ("Selector Switch", 18, 1)
    assert unit.image == catalog.IMAGE_HEATING
    names = unit.options["LevelNames"].split("|")
    assert names[1:] == [
        "Inactive",
        "Ready",
        "Delayed start",
        "Running",
        "Paused",
        "Action required",
        "Finished",
        "Aborting",
        "Error",
        "Offline",
    ]
    assert unit.options["LevelOffHidden"] == "true"
    assert unit.options["SelectorStyle"] == "1"
    assert unit.options["LevelActions"] == "|" * 10


def test_status_code_offline_unknown_and_dutch():
    app = app_with(**{catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.Run"})
    app.connected = False
    assert status_code(app).nvalue == 100
    assert status_code(model.Appliance(HA, connected=False)).nvalue == 100
    # An unknown or missing state leaves the level as it is: nothing is planned.
    odd = app_with(**{catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.Brewing"})
    assert status_code(odd) is None
    assert status_code(app_with()) is None
    nl = status_code(
        app_with(**{catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.Run"}), "nl"
    )
    assert nl.name == "$Oven 1 Statuscode"
    assert nl.options["LevelNames"].split("|")[4] == "Bezig"
    assert nl.options["LevelNames"].split("|")[10] == "Offline"


def test_status_code_writes_only_when_the_level_changes(domoticz):
    state = persistence.State()
    app = app_with(**{catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.Run"})

    def write():
        domoticz_api.apply_updates(
            domoticz.Devices, planner.plan_appliance(app, state, "en", NOW, False, "Oven 1"), state
        )

    write()
    unit = domoticz.Devices[HA].Units[planner.UNIT_STATUS_CODE]
    assert unit.stored["Used"] == 1 and unit.nValue == 40
    count = len(unit.updates)
    write()
    assert len(unit.updates) == count
    app.status[catalog.OPERATION_STATE] = model.Value("BSH.Common.EnumType.OperationState.Finished")
    write()
    assert unit.nValue == 70 and len(unit.updates) == count + 1
    unit.Used = 1  # the user shows it on the dashboard; that choice is kept
    write()
    assert unit.Used == 1


def test_fixed_unit_numbers_are_permanent():
    # A unit number can never change once created.
    assert (
        planner.UNIT_PROGRAM_SELECT,
        planner.UNIT_ACTIONS,
        planner.UNIT_ACK,
        planner.UNIT_STATUS_CODE,
        planner.UNIT_DELAYED_START,
    ) == (8, 9, 10, 11, 12)
    assert planner.UNIT_DELAYED_START < persistence.FIRST_DYNAMIC_UNIT
    assert set(planner.CONTROL_ONLY_UNITS) == {9, 10, 12}


def test_action_levels_are_a_fixed_contiguous_contract():
    assert planner.ACTION_LEVELS == {
        "start": 10,
        "stop": 20,
        "pause": 30,
        "resume": 40,
        "open_door": 50,
        "partly_open_door": 60,
    }
    # In level order, door actions last: leaving those off other appliances renumbers nothing.
    assert list(planner.ACTION_LEVELS.values()) == [10, 20, 30, 40, 50, 60]
    assert list(planner.ACTION_LEVELS)[-2:] == ["open_door", "partly_open_door"]


WASHER_A = "LaundryCare.Washer.Program.Cotton"
WASHER_B = "LaundryCare.Washer.Program.EasyCare"
READY = "BSH.Common.EnumType.OperationState.Ready"
RUN = "BSH.Common.EnumType.OperationState.Run"


def washer(state=READY):
    app = model.Appliance(HA, name="Washer 1", brand="BOSCH", type="Washer", connected=True)
    app.status[catalog.OPERATION_STATE] = model.Value(state)
    app.cache.programs = [WASHER_A, WASHER_B]
    app.names = {WASHER_A: "Katoen", WASHER_B: "Kreukherstellend"}
    app.cache.program_details = {
        WASHER_A: {"key": WASHER_A, "options": [{"key": catalog.FINISH_IN, "type": "Int"}]},
        WASHER_B: {"key": WASHER_B, "options": []},
    }
    return app


def controlled(app, state=None, lang="en"):
    state = state or persistence.State()
    updates = planner.plan_appliance(app, state, lang, NOW, False, app.name, allow_control=True)
    return state, by_unit(updates)


def test_program_selector_shows_the_selected_program_as_a_stable_level():
    app = washer()
    app.selected_program = WASHER_B
    state, units = plan(app)
    u = units[planner.UNIT_PROGRAM_SELECT]
    assert (u.type_name, u.switchtype, u.nvalue, u.svalue) == ("Selector Switch", 18, 20, "20")
    assert u.options["LevelNames"] == "-|Katoen|Kreukherstellend"
    assert u.options["HCKeys"] == f"|{WASHER_A}|{WASHER_B}"
    assert state.level_maps[HA]["8"] == [WASHER_A, WASHER_B]


def test_program_selector_shows_the_running_program_and_never_reorders_levels():
    app = washer(RUN)
    app.selected_program, app.active_program = WASHER_B, WASHER_A
    state = persistence.State(level_maps={HA: {"8": [WASHER_B]}})
    _, units = plan(app, state)
    assert units[planner.UNIT_PROGRAM_SELECT].nvalue == 20
    assert state.level_maps[HA]["8"] == [WASHER_B, WASHER_A]


def test_program_selector_is_level_zero_with_nothing_selected():
    _, units = plan(washer())
    assert units[planner.UNIT_PROGRAM_SELECT].nvalue == 0


def test_a_blocked_program_selector_is_never_planned():
    # Its level keys are lost (spec 4.4): a fresh map from today's program list would give a
    # stored scene or timer level another program, so it gets no levels and no HCKeys.
    app = washer()
    app.selected_program = WASHER_B
    state = persistence.State()
    blocked = {(HA, planner.UNIT_PROGRAM_SELECT)}
    for allow_control in (False, True):
        updates = planner.plan_appliance(
            app, state, "en", NOW, False, app.name, allow_control=allow_control, blocked=blocked
        )
        units = by_unit(updates)
        assert planner.UNIT_PROGRAM_SELECT not in units
        assert "8" not in state.level_maps.get(HA, {})
    assert planner.UNIT_ACTIONS in units


def test_a_blocked_dynamic_selector_is_never_planned():
    # As for unit 8: its lost level keys are never rebuilt from today's values (spec 4.4).
    state = persistence.State(unit_alloc={HA: {SPIN: 25}}, next_unit={HA: 26})
    updates = planner.plan_appliance(
        oven_with_details(), state, "en", NOW, False, "Oven 1", blocked={(HA, 25)}
    )
    assert 25 not in by_unit(updates)
    assert "25" not in state.level_maps.get(HA, {})


def test_control_units_exist_only_with_allow_control():
    app = washer()
    app.cache.commands = [catalog.ACKNOWLEDGE]
    _, off = plan(app)
    assert planner.UNIT_PROGRAM_SELECT in off
    assert not set(planner.CONTROL_ONLY_UNITS) & set(off)
    _, on = controlled(app)
    actions = on[planner.UNIT_ACTIONS]
    assert (actions.nvalue, actions.svalue) == (0, "0")
    assert actions.options["LevelNames"] == "-|Start|Stop|Pause|Resume"
    ack = on[planner.UNIT_ACK]
    assert (ack.type_name, ack.switchtype, ack.has_value) == ("Push On", 9, False)
    delayed = on[planner.UNIT_DELAYED_START]
    assert (delayed.name, delayed.nvalue) == ("Washer 1 Ready at", 0)
    names = delayed.options["LevelNames"].split("|")
    assert names[:3] == ["-", planner.clock_text(0), planner.clock_text(planner.DELAY_STEP_MIN)]
    assert len(names) == len(planner.delay_times()) + 1


def test_delayed_start_shows_the_held_time_and_follows_the_offered_option():
    app = washer()
    level = planner.delay_level(17 * 60)
    state = persistence.State(delayed_start={HA: 17 * 60})
    _, units = controlled(app, state)
    delayed = units[planner.UNIT_DELAYED_START]
    assert delayed.nvalue == level
    assert delayed.options["LevelNames"].split("|")[level // 10] == "17:00"
    oven = app_with(**{catalog.OPERATION_STATE: READY})
    oven.cache.programs = ["P.A"]
    oven.cache.program_details["P.A"] = {"options": [{"key": catalog.START_IN, "type": "Int"}]}
    _, units = controlled(oven, lang="nl")
    assert units[planner.UNIT_DELAYED_START].name == "Oven 1 Start om"
    oven.cache.program_details["P.A"] = {"options": []}
    _, units = controlled(oven)
    assert planner.UNIT_DELAYED_START not in units


def test_appliance_without_programs_gets_no_program_or_action_units():
    fridge = model.Appliance(HA, name="Fridge", type="FridgeFreezer", connected=True)
    fridge.cache.commands = [catalog.ACKNOWLEDGE]
    _, units = controlled(fridge)
    assert planner.UNIT_PROGRAM_SELECT not in units and planner.UNIT_ACTIONS not in units
    assert planner.UNIT_ACK in units


def test_delay_levels_round_trip():
    times = planner.delay_times()
    assert times[0] == 0 and (24 * 60) % planner.DELAY_STEP_MIN == 0
    for level in range(10, 10 * (len(times) + 1), 10):
        assert planner.delay_level(planner.delay_minutes(level)) == level
    for bad in (0, 15, 10 * (len(times) + 1)):
        assert planner.delay_minutes(bad) is None
    assert planner.delay_level(None) == 0 and planner.delay_level(7) == 0


def test_overlay_replaces_the_planned_value_until_it_is_gone():
    update = planner.DeviceUpdate(HA, 25, "x", "Set Point", 0, "180.0", has_value=False)
    shown = planner.apply_overlay([update], {(HA, 25): (0, "200.0")})
    assert (shown[0].svalue, shown[0].has_value) == ("200.0", True)
    assert planner.apply_overlay([update], {}) == [update]


def test_number_and_constraints_helpers_are_bounded_and_total():
    assert planner.finite_number(5) == 5.0 and planner.finite_number(10**15) is None
    assert planner.finite_number(True) is None and planner.finite_number(float("inf")) is None
    assert planner.constraints_of(None) == {} and planner.constraints_of({"constraints": 1}) == {}
    assert planner.constraints_of({"constraints": {"min": 1}}) == {"min": 1}


def test_only_an_appliance_listing_door_commands_gets_the_door_actions_after_the_others():
    oven = app_with(**{catalog.OPERATION_STATE: READY})
    oven.cache.programs = ["P.A"]
    _, units = controlled(oven)
    assert units[planner.UNIT_ACTIONS].options["LevelNames"] == "-|Start|Stop|Pause|Resume"
    oven.cache.commands = [catalog.PARTLY_OPEN_DOOR]
    _, units = controlled(oven)
    names = units[planner.UNIT_ACTIONS].options["LevelNames"].split("|")
    assert names == ["-", "Start", "Stop", "Pause", "Resume", "Open door", "Partly open door"]
    for name, level in planner.ACTION_LEVELS.items():
        assert names[level // 10] == catalog.word(f"action_{name}", "en")
    door = washer()
    door.cache.commands = [catalog.OPEN_DOOR]
    _, units = controlled(door)
    assert units[planner.UNIT_ACTIONS].options["LevelNames"].endswith("|Partly open door")


def test_out_of_range_progress_is_clamped_into_percent_range():
    for reported, shown in ((-2666, "0"), (140, "100"), (42, "42")):
        app = oven_with_details()
        app.active_program = "P.A"
        app.status[catalog.OPERATION_STATE] = model.Value("BSH.Common.EnumType.OperationState.Run")
        app.options[catalog.PROGRESS] = model.Value(reported, "%")
        _, units = plan(app)
        assert units[planner.UNIT_PROGRESS].svalue == shown, reported


def test_delayed_start_options_get_no_dynamic_unit():
    app = oven_with_details()
    app.options[catalog.START_IN] = model.Value(3600, "seconds")
    app.options[catalog.FINISH_IN] = model.Value(3600, "seconds")
    state, _ = plan(app)
    allocated = state.unit_alloc.get(app.ha_id, {})
    assert catalog.START_IN not in allocated and catalog.FINISH_IN not in allocated


# -- descriptions -------------------------------------------------------------------------------

CHILD_LOCK = "BSH.Common.Setting.ChildLock"
SABBATH = "Cooking.Oven.Setting.SabbathMode"
FORECAST = "BSH.Common.Option.EnergyForecast"
SALT = "Dishcare.Dishwasher.Event.SaltNearlyEmpty"
FAVOURITE = "BSH.Common.Event.Favorite.001.ExternalTrigger"
CAVITY = "Cooking.Oven.Status.CurrentCavityTemperature"


def every_kind():
    """An appliance that gets every kind of unit the planner makes."""
    app = washer(RUN)
    app.status[catalog.DOOR_STATE] = model.Value("BSH.Common.EnumType.DoorState.Closed")
    app.status[catalog.REMOTE_START] = model.Value(True)
    app.status[CAVITY] = model.Value(180.0, "°C")
    app.settings[catalog.POWER_STATE] = model.Value("BSH.Common.EnumType.PowerState.On")
    app.settings[CHILD_LOCK] = model.Value(False)
    app.cache.setting_constraints[CHILD_LOCK] = {"access": "readWrite"}
    app.settings[SABBATH] = model.Value(False)
    app.cache.setting_constraints[SABBATH] = {"access": "read"}
    app.cache.program_details[WASHER_A]["options"] += [
        {"key": SPIN, "type": "Int", "constraints": {"min": 400, "max": 1400}},
        {"key": FORECAST, "type": "Int", "unit": "%", "constraints": {"access": "read"}},
    ]
    app.selected_program = app.active_program = WASHER_A
    app.options[SPIN] = model.Value(1200)
    app.options[FORECAST] = model.Value(40, "%")
    app.options[catalog.PROGRESS] = model.Value(30, "%")
    app.options[catalog.REMAINING] = model.Value(600, "seconds")
    app.cache.commands = [catalog.ACKNOWLEDGE]
    app.events[SALT] = model.EventState(model.PRESENT, "hint", "")
    app.events[FAVOURITE] = model.EventState(model.PRESENT, "hint", "")
    return app


def described(lang):
    state = persistence.State()
    app = every_kind()
    updates = planner.plan_appliance(app, state, lang, NOW, False, app.name, allow_control=True)
    account = planner.plan_account(planner.AccountView("AUTHORIZED"), lang)
    return state, by_unit(updates), by_unit(account, planner.ACCOUNT_DEVICE)


def test_every_unit_gets_a_short_description_in_both_languages():
    for lang in ("en", "nl"):
        state, units, account = described(lang)
        assert set(range(1, 13)) <= set(units)
        for update in [*units.values(), *account.values()]:
            assert update.description, (lang, update.name)
            assert chr(0x2014) not in update.description
            assert len(update.description) <= 260, (lang, update.name)
            # One or two sentences: the edit dialog is small.
            assert update.description.count(". ") <= 2, (lang, update.name)
    _, en, _ = described("en")
    _, nl, _ = described("nl")
    for number in en:
        assert en[number].description != nl[number].description, number


def by_key(units):
    return {u.options.get("HCKey"): u for u in units.values()}


def test_writable_units_say_they_work_only_with_allow_control():
    for lang, phrase in (("en", "Allow control is on"), ("nl", "Allow control aan staat")):
        state, units, account = described(lang)
        keys = by_key(units)
        writable = [units[n] for n in (2, 8, 9, 10, 12)] + [keys[CHILD_LOCK], keys[SPIN]]
        for update in writable:
            assert phrase in update.description, (lang, update.name)
        read_only = [units[n] for n in (1, 3, 4, 5, 6, 7, 11)]
        read_only += [keys[SABBATH], keys[FORECAST], keys[CAVITY], keys[SALT], keys[FAVOURITE]]
        for update in read_only:
            assert phrase not in update.description, (lang, update.name)


def test_descriptions_explain_the_cards_users_mix_up():
    _, units, account = described("nl")
    assert "(gekozen)" in units[planner.UNIT_PROGRAM].description
    assert "Programmakeuze" in units[planner.UNIT_PROGRAM].description
    assert "start" in units[planner.UNIT_PROGRAM_SELECT].description
    assert "Acties" in units[planner.UNIT_DELAYED_START].description
    assert '"-"' in units[planner.UNIT_DELAYED_START].description
    assert '"-"' in units[planner.UNIT_ACTIONS].description
    assert "melding" in units[planner.UNIT_ACK].description
    code = units[planner.UNIT_STATUS_CODE].description
    for state, level in planner.STATUS_CODE_LEVELS.items():
        assert f"{level} {catalog.state_name(state, 'nl')}" in code
    assert f"{planner.STATUS_CODE_OFFLINE} Offline" in code
    assert "Instellingen, Apparaten" in account[planner.ACCOUNT_TEXT].description


def test_ready_at_and_start_at_are_described_apart():
    _, units, _ = described("en")
    oven = app_with(**{catalog.OPERATION_STATE: READY})
    oven.cache.programs = ["Cooking.Oven.Program.HeatingMode.HotAir"]
    oven.cache.program_details = {
        "Cooking.Oven.Program.HeatingMode.HotAir": {"options": [{"key": catalog.START_IN}]}
    }
    _, oven_units = controlled(oven)
    ready_at = units[planner.UNIT_DELAYED_START].description
    start_at = oven_units[planner.UNIT_DELAYED_START].description
    assert "finish time" in ready_at and "start time" in start_at
    assert ready_at != start_at


# -- Dutch words --------------------------------------------------------------------------------

COLOUR = "BSH.Common.Setting.AmbientLightColor"
COLOUR_VALUE = "BSH.Common.EnumType.AmbientLightColor."


def test_ambient_colour_levels_are_named_in_the_device_language():
    app = app_with()
    app.settings[COLOUR] = model.Value(COLOUR_VALUE + "Color40")
    values = [COLOUR_VALUE + v for v in ("CustomColor", "Color30", "Color40")]
    app.cache.setting_constraints[COLOUR] = {"access": "readWrite", "allowedvalues": values}
    app.value_names[COLOUR_VALUE + "Color40"] = "koud wit"
    app.value_names[COLOUR_VALUE + "CustomColor"] = "Custom color"
    _, units = plan(app, lang="nl")
    [colour] = [u for n, u in units.items() if n >= 20]
    assert colour.options["LevelNames"] == "Niet beschikbaar|Eigen kleur|Kleur 30|koud wit"


def test_read_only_enum_text_uses_catalog_value_names():
    app = app_with(**{"X.Y.Status.Colour": COLOUR_VALUE + "Color31"})
    _, units = plan(app, lang="nl")
    assert dynamic(units)["Colour"].svalue == "Kleur 31"


def test_duration_and_calls_are_shown_in_the_device_language():
    app = app_with(**{catalog.OPERATION_STATE: READY})
    app.cache.programs = ["P.A"]
    app.cache.program_details = {"P.A": {"options": []}}
    app.selected_program = "P.A"
    app.cache.program_details["P.A"]["options"] = [
        {"key": "BSH.Common.Option.Duration", "type": "Int", "unit": "seconds"}
    ]
    app.options["BSH.Common.Option.Duration"] = model.Value(600, "seconds")
    _, units = plan(app, lang="nl")
    [duration] = [u for n, u in units.items() if n >= 20]
    assert (duration.options["ValueUnit"], duration.options["ValueStep"]) == ("min", "1")
    account = by_unit(
        planner.plan_account(planner.AccountView("AUTHORIZED"), "nl"), planner.ACCOUNT_DEVICE
    )
    assert account[planner.ACCOUNT_CALLS].options["Custom"] == "1;aanroepen"


# -- clutter ------------------------------------------------------------------------------------


def test_new_setting_devices_are_created_hidden_and_the_rest_shown():
    _, units, _ = described("en")
    keys = by_key(units)
    hidden = (CHILD_LOCK, SABBATH, FORECAST, SPIN)
    assert [keys[key].used for key in hidden] == [0] * len(hidden)
    for key in (CAVITY, SALT, FAVOURITE):
        assert keys[key].used == 1, key
    shown = [n for n in units if n < 20 and n != planner.UNIT_STATUS_CODE]
    assert all(units[n].used == 1 for n in shown)


def test_a_setting_device_the_user_shows_stays_shown(domoticz):
    app = app_with()
    app.settings[CHILD_LOCK] = model.Value(False)
    app.cache.setting_constraints[CHILD_LOCK] = {"access": "readWrite"}
    state = persistence.State()
    updates = planner.plan_appliance(app, state, "en", NOW, False, app.name)
    domoticz_api.apply_updates(domoticz.Devices, updates, state)
    [unit] = [u for n, u in domoticz.Devices[HA].Units.items() if n >= 20]
    assert unit.stored["Used"] == 0
    unit.Used = 1  # the user adds it to the dashboard
    unit.Update(Log=False, UpdateProperties=True)
    app.settings[CHILD_LOCK] = model.Value(True)
    updates = planner.plan_appliance(app, state, "en", NOW, False, app.name)
    domoticz_api.apply_updates(domoticz.Devices, updates, state)
    assert (unit.stored["Used"], unit.stored["sValue"]) == (1, "On")


def test_cooling_setpoints_and_super_modes_stay_shown():
    fridge = model.Appliance(
        HA, name="Fridge 1", brand="BOSCH", type="FridgeFreezer", connected=True
    )
    keys = {
        "Refrigeration.FridgeFreezer.Setting.SetpointTemperatureFreezer": model.Value(-18, "°C"),
        "Refrigeration.FridgeFreezer.Setting.SetpointTemperatureRefrigerator": model.Value(4, "°C"),
        "Refrigeration.FridgeFreezer.Setting.SuperModeFreezer": model.Value(False),
        "Refrigeration.FridgeFreezer.Setting.SuperModeRefrigerator": model.Value(False),
        CHILD_LOCK: model.Value(False),
    }
    for key, value in keys.items():
        fridge.settings[key] = value
        fridge.cache.setting_constraints[key] = {"access": "readWrite"}
    state = persistence.State()
    used = {
        u.options["HCKey"]: u.used
        for u in planner.plan_appliance(fridge, state, "en", NOW, False, fridge.name)
        if "HCKey" in u.options
    }
    assert used == {key: 0 if key == CHILD_LOCK else 1 for key in keys}


def test_descriptions_say_what_the_unit_really_does():
    _, en, en_account = described("en")
    _, nl, nl_account = described("nl")
    assert en[planner.UNIT_REMOTE].description == (
        "Whether the appliance accepts a start from Domoticz; switch remote start on at the "
        "appliance itself."
    )
    assert nl[planner.UNIT_REMOTE].description == (
        "Of het apparaat een start vanuit Domoticz accepteert; zet starten op afstand aan op het "
        "apparaat zelf."
    )
    assert en[planner.UNIT_POWER].description.startswith(
        "Switches the appliance on or off; some appliances can be switched on here but not off."
    )
    assert nl[planner.UNIT_POWER].description.startswith(
        "Zet het apparaat aan of uit; sommige apparaten kun je hier wel aanzetten, maar niet "
        "uitzetten."
    )
    # The washer offers FinishInRelative: a finish time.
    assert nl[planner.UNIT_DELAYED_START].description.startswith(
        'Kies een eindtijd en daarna Start in Acties; bij "-" start het programma meteen. '
        "Een gekozen tijd geldt voor één keer."
    )
    assert en[planner.UNIT_DELAYED_START].description.startswith(
        'Pick a finish time, then choose Start in Actions; with "-" Start begins straight away. '
        "A chosen time applies to one run only."
    )
    assert nl[planner.UNIT_DOOR].description.endswith('toont dan "Vergrendeld".')
    assert en[planner.UNIT_DOOR].description.endswith('then shows "Locked".')
    assert nl[planner.UNIT_PROGRAM_SELECT].description.startswith(
        "Kies een programma; kiezen start het nog niet, dat doe je met Start in Acties."
    )
    assert "aanmelden" in nl_account[planner.ACCOUNT_TEXT].description
    assert "inloggen" not in nl_account[planner.ACCOUNT_TEXT].description
    assert nl_account[planner.ACCOUNT_ACTIONS].description.startswith(
        '"Opnieuw aanmelden" toont een nieuwe aanmeldlink en code; "Apparaten verversen" laadt'
    )
    assert en_account[planner.ACCOUNT_ACTIONS].description.startswith(
        '"Sign in again" shows a new sign-in link and code;'
    )
    assert nl_account[planner.ACCOUNT_CALLS].description == (
        "Aanroepen naar Home Connect in de afgelopen 24 uur; Home Connect staat er 1000 per dag "
        "toe."
    )


def test_start_at_description_names_a_start_time():
    oven = app_with(**{catalog.OPERATION_STATE: READY})
    oven.cache.programs = ["Cooking.Oven.Program.HeatingMode.HotAir"]
    oven.cache.program_details = {
        "Cooking.Oven.Program.HeatingMode.HotAir": {"options": [{"key": catalog.START_IN}]}
    }
    oven.cache.commands = [catalog.OPEN_DOOR]
    _, units = controlled(oven, lang="nl")
    assert units[planner.UNIT_DELAYED_START].description.startswith(
        'Kies een starttijd en daarna Start in Acties; bij "-" start het programma meteen.'
    )
    assert "ovendeur" in units[planner.UNIT_ACTIONS].description


def test_actions_mention_door_actions_only_where_offered():
    for lang, door in (("en", "door"), ("nl", "deur")):
        _, units, _ = described(lang)  # a washer: no door commands
        assert door not in units[planner.UNIT_ACTIONS].description, lang


def test_descriptions_contain_no_markup_characters():
    # Domoticz HTML-escapes a description when it serves it, so "<", ">" and "&" would show up
    # literally as entities.
    for lang in ("en", "nl"):
        for name in catalog._DESCRIPTIONS:
            text = catalog.description(name, lang, True, levels="")
            assert not set("<>&") & set(text), (lang, name)


WASHER_HA = "SIEMENS-HCS03WCH1-000000000003"
ENERGY_TOTAL = "BSH.Common.Status.Program.All.Energy.Consumed"
WATER_TOTAL = "BSH.Common.Status.Program.All.Water.Consumed"
DETERGENT_TOTAL = "LaundryCare.Washer.Status.Detergent.All.Consumed"


def washer_with(**status):
    app = model.Appliance(WASHER_HA, name="Washer", type="Washer", connected=True)
    app.status.update(status)
    return app


def plan_washer(app, state=None, lang="en"):
    state = state or persistence.State()
    return by_unit(planner.plan_appliance(app, state, lang, NOW, False, app.name), WASHER_HA)


def units_for(units, key):
    return [u for u in units.values() if u.options.get("HCKey") == key]


def unit_for(units, key):
    (unit,) = units_for(units, key)
    return unit


def test_lifetime_totals_become_meters():
    app = washer_with(
        **{
            ENERGY_TOTAL: model.Value(6293, "Wh"),
            WATER_TOTAL: model.Value(911000, "ml"),
            DETERGENT_TOTAL: model.Value(533, "ml"),
        }
    )
    units = plan_washer(app)
    energy, water, detergent = (
        unit_for(units, k) for k in (ENERGY_TOTAL, WATER_TOTAL, DETERGENT_TOTAL)
    )
    assert (energy.type_name, energy.type_code, energy.switchtype, energy.svalue) == (
        "kWh",
        None,
        0,
        "0;6293",
    )
    assert "EnergyMeterMode" not in energy.options
    assert (energy.options["HCKey"], energy.options["HCKind"]) == (ENERGY_TOTAL, "energy_total")
    assert (water.type_code, water.switchtype, water.svalue) == ((113, 0), 3, "911")
    assert (water.options["ValueQuantity"], water.options["ValueUnits"]) == ("Water", "L")
    assert (water.options["HCKey"], water.options["HCKind"]) == (WATER_TOTAL, "counter_total")
    assert (detergent.svalue, detergent.options["ValueUnits"]) == ("533", "ml")
    assert water.description == catalog.description("total", "en")
    nl = unit_for(plan_washer(app, lang="nl"), DETERGENT_TOTAL)
    assert nl.options["ValueQuantity"] == "Wasmiddel"


def test_meter_is_created_only_with_a_reported_total():
    # Created empty, Domoticz would log 0 and count the whole lifetime as the first day's use.
    units = plan_washer(washer_with(**{ENERGY_TOTAL: model.Value(None, "Wh")}))
    meters = units_for(units, ENERGY_TOTAL)
    assert len(meters) == 1
    assert (meters[0].create, meters[0].has_value) == (False, False)


def test_meter_total_in_an_unexpected_unit_is_not_written():
    # A kWh total divided as if it were Wh would be 1000x off, and a meter's history is permanent.
    units = plan_washer(washer_with(**{ENERGY_TOTAL: model.Value(6.3, "kWh")}))
    meters = units_for(units, ENERGY_TOTAL)
    assert len(meters) == 1
    assert (meters[0].create, meters[0].has_value) == (False, False)


def test_meter_total_without_a_unit_is_still_written():
    units = plan_washer(washer_with(**{ENERGY_TOTAL: model.Value(6293, "")}))
    assert unit_for(units, ENERGY_TOTAL).svalue == "0;6293"


def test_meter_values_are_whole_numbers_and_non_numbers_write_nothing():
    app = washer_with(
        **{ENERGY_TOTAL: model.Value(5974.0, "Wh"), WATER_TOTAL: model.Value(849400, "ml")}
    )
    units = plan_washer(app)
    assert unit_for(units, ENERGY_TOTAL).svalue == "0;5974"
    assert unit_for(units, WATER_TOTAL).svalue == "849"
    units = plan_washer(washer_with(**{ENERGY_TOTAL: model.Value("n/a", "Wh")}))
    meters = units_for(units, ENERGY_TOTAL)
    assert len(meters) == 1
    assert (meters[0].create, meters[0].has_value) == (False, False)


def test_existing_value_device_keeps_its_kind():
    app = washer_with(**{ENERGY_TOTAL: model.Value(6293, "Wh")})
    state = persistence.State()
    state.unit_alloc[WASHER_HA] = {ENERGY_TOTAL: 40}
    state.unit_kinds[WASHER_HA] = {"40": "custom"}
    unit = unit_for(plan_washer(app, state), ENERGY_TOTAL)
    assert (unit.unit, unit.type_name, unit.type_code, unit.svalue) == (40, "Custom", None, "6293")


def test_diagnostics_and_duplicates_are_created_unused_even_when_named():
    app = app_with()
    diagnostics = {
        "BSH.Common.Status.InteriorIlluminationActive": model.Value(False),
        "Cooking.Oven.Status.LastRemoteStartRelease": model.Value("2026-10-23T16:54:48"),
        "Dishcare.Dishwasher.Status.EcoDryActive": model.Value(False),
        "LaundryCare.Dryer.Status.ConnectedDry.OperationStatus": model.Value("Available"),
    }
    options = {
        "LaundryCare.Common.Option.ReferToProgram": model.Value(0),
        "LaundryCare.Dryer.Option.WrinkleGuard": model.Value("Min120"),
        "BSH.Common.Option.EstimatedTotalProgramTime": model.Value(3600, "seconds"),
        "BSH.Common.Option.EnergyForecast": model.Value(61, "%"),
        "BSH.Common.Option.WaterForecast": model.Value(49, "%"),
        "BSH.Common.Option.SmartEnergyService.SmartStartEnabled": model.Value(False),
        "Dishcare.Dishwasher.Option.SilenceOnDemand": model.Value(False),
    }
    app.status.update(diagnostics)
    _, units = plan(app)
    used = {u.options["HCKey"]: u.used for n, u in units.items() if n >= 20}
    assert used == dict.fromkeys(diagnostics, 0)
    # Options get a device only under a program; the planner path is covered by the forecast in
    # test_new_setting_devices_are_created_hidden_and_the_rest_shown.
    for key in [*diagnostics, *options]:
        assert catalog.known(key), key  # unused by choice, not for want of a name
        assert not catalog.used_at_creation(key), key


def test_the_program_phase_is_reset_once_the_run_is_over():
    phase = "Dishcare.Dishwasher.Status.ProgramPhase"
    drying = "Dishcare.Dishwasher.EnumType.ProgramPhase.Drying"
    app = app_with(
        **{catalog.OPERATION_STATE: "BSH.Common.EnumType.OperationState.Run", phase: drying}
    )
    app.value_names[drying] = "Drying active"
    app.active_program = "Dishcare.Dishwasher.Program.Auto2"
    state, units = plan(app)
    assert by_key(units)[phase].svalue == "Drying active"
    app.status[catalog.OPERATION_STATE] = model.Value(READY)
    app.active_program = None
    for lang, shown in (("en", "Not running"), ("nl", "Niet actief")):
        assert by_key(plan(app, state, lang=lang)[1])[phase].svalue == shown
    assert app.status[phase].value == drying  # the model keeps what Home Connect reported


def test_only_the_main_options_start_used():
    for key in (
        "Cooking.Oven.Option.SetpointTemperature",
        "BSH.Common.Option.Duration",
        "LaundryCare.Common.Option.ProcessPhase",
    ):
        assert catalog.used_at_creation(key), key
    assert not catalog.used_at_creation("Dishcare.Dishwasher.Option.HygienePlus")


def test_the_remote_control_card_is_shown_only_with_allow_control():
    app = app_with(**{catalog.REMOTE_START: True})
    _, off = plan(app)
    assert off[planner.UNIT_REMOTE].used == 0
    on = planner.plan_appliance(
        app, persistence.State(), "en", NOW, False, app.name, allow_control=True
    )
    assert by_unit(on)[planner.UNIT_REMOTE].used == 1


def test_a_program_phase_left_from_before_a_restart_is_reset_but_never_created():
    phase = "Dishcare.Dishwasher.Status.ProgramPhase"
    app = app_with(**{catalog.OPERATION_STATE: READY})
    state, units = plan(app)
    assert phase not in by_key(units)
    state.unit_alloc.setdefault(HA, {})[phase] = 30
    reset = by_key(plan(app, state)[1])[phase]
    assert (reset.unit, reset.svalue, reset.create) == (30, "Not running", False)
