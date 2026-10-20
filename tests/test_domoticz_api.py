import ast
from pathlib import Path

import DomoticzEx as Domoticz

import domoticz_api
import persistence
import planner

SELECTOR = "Selector Switch"


def up(unit, svalue="x", nvalue=0, dev="D1", name=None, **kw):
    return planner.DeviceUpdate(
        dev, unit, name or f"Dev {unit}", kw.pop("type_name", "Text"), nvalue, svalue, **kw
    )


def make_unit(dev, number, options, type_name="Text", **kw):
    unit = Domoticz.Unit(
        Name=f"u{number}", DeviceID=dev, Unit=number, TypeName=type_name, Options=options, **kw
    )
    unit.Create()
    return unit


def test_creates_in_ascending_order_and_records_names(domoticz):
    state = persistence.State()
    created = domoticz_api.apply_updates(domoticz.Devices, [up(21), up(1), up(3)], state)
    assert created == 3
    assert list(domoticz.Devices["D1"].Units) == [1, 3, 21]
    assert state.auto_names["D1:1"] == "Dev 1"
    assert domoticz.Devices["D1"].Units[1].stored["sValue"] == "x"


def test_allow_create_false_creates_nothing(domoticz):
    state = persistence.State()
    assert domoticz_api.apply_updates(domoticz.Devices, [up(1)], state, allow_create=False) == 0
    assert "D1" not in domoticz.Devices


def test_no_write_when_unchanged_and_no_typename_ever(domoticz):
    state = persistence.State()
    domoticz_api.apply_updates(domoticz.Devices, [up(1)], state)
    unit = domoticz.Devices["D1"].Units[1]
    before = len(unit.updates)
    domoticz_api.apply_updates(domoticz.Devices, [up(1)], state)
    assert len(unit.updates) == before
    domoticz_api.apply_updates(domoticz.Devices, [up(1, svalue="y", type_name="Alert")], state)
    assert all("TypeName" not in kw for kw in unit.updates)
    assert unit.stored["sValue"] == "y"


def test_deadband_suppresses_noise(domoticz):
    state = persistence.State()
    t20 = {"type_name": "Temperature", "deadband": 0.5}
    domoticz_api.apply_updates(domoticz.Devices, [up(20, "23", **t20)], state)
    unit = domoticz.Devices["D1"].Units[20]
    n = len(unit.updates)
    domoticz_api.apply_updates(domoticz.Devices, [up(20, "23.4", **t20)], state)
    assert len(unit.updates) == n
    domoticz_api.apply_updates(domoticz.Devices, [up(20, "24", **t20)], state)
    assert unit.stored["sValue"] == "24"


def test_deadband_with_a_non_numeric_old_value_writes(domoticz):
    state = persistence.State()
    t20 = {"type_name": "Temperature", "deadband": 0.5}
    domoticz_api.apply_updates(domoticz.Devices, [up(20, "n/a", **t20)], state)
    domoticz_api.apply_updates(domoticz.Devices, [up(20, "23.1", **t20)], state)
    assert domoticz.Devices["D1"].Units[20].stored["sValue"] == "23.1"


def test_empty_numeric_value_is_never_written(domoticz):
    state = persistence.State()
    domoticz_api.apply_updates(domoticz.Devices, [up(20, "", type_name="Set Point")], state)
    unit = domoticz.Devices["D1"].Units[20]
    assert unit.updates == []
    unit.nValue, unit.sValue = 0, "21"
    domoticz_api.apply_updates(domoticz.Devices, [up(20, "", type_name="Set Point")], state)
    assert unit.updates == []


def test_has_value_false_creates_without_value(domoticz):
    state = persistence.State()
    update = up(20, "7", 3, type_name="Text", has_value=False, used=0)
    assert domoticz_api.apply_updates(domoticz.Devices, [update], state) == 1
    unit = domoticz.Devices["D1"].Units[20]
    assert unit.updates == []
    assert unit.stored["sValue"] == "" and unit.stored["nValue"] == 0
    assert unit.stored["Used"] == 0
    assert state.auto_names["D1:20"] == "Dev 20"


def test_has_value_false_never_overwrites_an_existing_value(domoticz):
    state = persistence.State()
    domoticz_api.apply_updates(domoticz.Devices, [up(20, "keep", 5)], state)
    unit = domoticz.Devices["D1"].Units[20]
    n = len(unit.updates)
    domoticz_api.apply_updates(domoticz.Devices, [up(20, "new", 9, has_value=False)], state)
    assert len(unit.updates) == n
    assert (unit.stored["nValue"], unit.stored["sValue"]) == (5, "keep")


def test_has_value_false_still_rewrites_options(domoticz):
    state = persistence.State()
    first = up(20, "", type_name=SELECTOR, options={"LevelNames": "a|b"}, has_value=False)
    domoticz_api.apply_updates(domoticz.Devices, [first], state)
    second = up(20, "", type_name=SELECTOR, options={"LevelNames": "a|b|c"}, has_value=False)
    domoticz_api.apply_updates(domoticz.Devices, [second], state)
    unit = domoticz.Devices["D1"].Units[20]
    assert unit.stored["Options"] == {"LevelNames": "a|b|c"}
    assert [kw for kw in unit.updates if "UpdateOptions" not in kw] == []


def test_has_value_true_writes_a_selector_level_zero(domoticz):
    state = persistence.State()
    domoticz_api.apply_updates(domoticz.Devices, [up(8, "0", 0, type_name=SELECTOR)], state)
    domoticz_api.apply_updates(domoticz.Devices, [up(8, "10", 10, type_name=SELECTOR)], state)
    assert domoticz.Devices["D1"].Units[8].stored["sValue"] == "10"


def test_user_rename_survives(domoticz):
    state = persistence.State()
    domoticz_api.apply_updates(domoticz.Devices, [up(1, name="Oven State")], state)
    unit = domoticz.Devices["D1"].Units[1]
    domoticz_api.apply_updates(domoticz.Devices, [up(1, name="Oven Status")], state)
    assert unit.stored["Name"] == "Oven Status"
    unit.Name = "My oven"
    domoticz_api.apply_updates(domoticz.Devices, [up(1, name="Oven Toestand")], state)
    assert unit.Name == "My oven"


def test_options_change_uses_the_options_flag(domoticz):
    state = persistence.State()
    sel = {"type_name": SELECTOR}
    first = up(20, "10", 10, options={"LevelNames": "a|b"}, **sel)
    domoticz_api.apply_updates(domoticz.Devices, [first], state)
    second = up(20, "10", 10, options={"LevelNames": "a|b|c"}, **sel)
    domoticz_api.apply_updates(domoticz.Devices, [second], state)
    assert domoticz.Devices["D1"].Units[20].stored["Options"] == {"LevelNames": "a|b|c"}


def test_options_restored_after_user_edit_dropped_custom_keys(domoticz):
    planned = {
        "LevelNames": "-|Eco|Cotton",
        "LevelOffHidden": "false",
        "SelectorStyle": "1",
        "HCKey": "A.Option.B",
        "HCKind": "selector",
        "HCKeys": "|Eco|Cotton",
    }
    state = persistence.State()
    update = up(20, "10", 10, type_name=SELECTOR, options=planned)
    domoticz_api.apply_updates(domoticz.Devices, [update], state)
    unit = domoticz.Devices["D1"].Units[20]
    # What the selector dialog saves: custom keys gone, a level renamed by the user.
    unit.Options = {"LevelNames": "-|Mine|Cotton", "LevelOffHidden": "false", "SelectorStyle": "1"}
    unit.stored["Options"] = dict(unit.Options)
    domoticz_api.apply_updates(domoticz.Devices, [update], state)
    assert unit.stored["Options"] == planned
    n = len(unit.updates)
    domoticz_api.apply_updates(domoticz.Devices, [update], state)
    assert len(unit.updates) == n


def test_set_used(domoticz):
    state = persistence.State()
    domoticz_api.apply_updates(
        domoticz.Devices, [up(1, dev="account"), up(2, dev="account")], state
    )
    domoticz_api.set_used(domoticz.Devices, "account", [1, 2], False)
    assert domoticz.Devices["account"].Units[1].stored["Used"] == 0
    domoticz_api.set_used(domoticz.Devices, "account", [1, 2], True)
    assert domoticz.Devices["account"].Units[2].stored["Used"] == 1
    domoticz_api.set_used(domoticz.Devices, "missing", [1], True)


def test_state_round_trip(domoticz):
    state = persistence.State(next_unit={"D1": 25})
    domoticz_api.save_state(state)
    loaded, fell_back = domoticz_api.load_state()
    assert (loaded.next_unit, fell_back) == ({"D1": 25}, False)


def test_load_state_with_nothing_stored_falls_back(domoticz):
    loaded, fell_back = domoticz_api.load_state()
    assert fell_back is True
    assert loaded == persistence.State()


def test_recover_from_devices(domoticz):
    make_unit(
        "H1", 30,
        {"HCKey": "A.Option.B", "HCKind": "selector", "HCKeys": "|V1|V2"},
        SELECTOR, Switchtype=18,
    )  # fmt: skip
    make_unit("H1", 31, {"HCKey": "A.Option.C", "HCKind": "selector"}, SELECTOR, Switchtype=18)
    make_unit("account", 2, {})
    state = persistence.State()
    blocked = domoticz_api.recover_from_devices(domoticz.Devices, state)
    assert state.unit_alloc["H1"] == {"A.Option.B": 30, "A.Option.C": 31}
    assert state.level_maps["H1"]["30"] == ["V1", "V2"]
    assert state.unit_kinds["H1"]["30"] == "selector"
    assert state.next_unit["H1"] == 32
    assert blocked == [("H1", 31)]
    assert "account" not in state.next_unit


def test_recover_never_overrides_existing_state(domoticz):
    make_unit("H1", 30, {"HCKey": "K", "HCKind": "text"})
    state = persistence.State(unit_alloc={"H1": {"K": 30}}, next_unit={"H1": 40})
    domoticz_api.recover_from_devices(domoticz.Devices, state)
    assert state.next_unit["H1"] == 40


def test_recover_never_reads_level_names(domoticz):
    make_unit(
        "H1", 30,
        {"HCKind": "selector", "HCKey": "K", "LevelNames": "-|Eco|Cotton"},
        SELECTOR,
    )  # fmt: skip
    state = persistence.State()
    blocked = domoticz_api.recover_from_devices(domoticz.Devices, state)
    assert "30" not in state.level_maps.get("H1", {})
    assert blocked == [("H1", 30)]


def test_reconcile_on_a_normal_load_raises_next_unit(domoticz):
    """A clean load (no fallback) still reconciles: a unit above next_unit must not be reused."""
    saved = persistence.State(next_unit={"H1": 22}, unit_alloc={"H1": {"K": 21}})
    domoticz_api.save_state(saved)
    make_unit("H1", 40, {"HCKey": "Z", "HCKind": "text"})
    state, fell_back = domoticz_api.load_state()
    assert fell_back is False
    domoticz_api.recover_from_devices(domoticz.Devices, state)
    assert state.next_unit["H1"] == 41
    assert state.unit_alloc["H1"] == {"K": 21, "Z": 40}


def test_reconcile_on_a_normal_load_recovers_a_missing_level_map(domoticz):
    saved = persistence.State(next_unit={"H1": 31})
    domoticz_api.save_state(saved)
    make_unit(
        "H1", 30,
        {"HCKey": "K", "HCKind": "selector", "HCKeys": "|A|B|C", "LevelNames": "-|x|y|z"},
        SELECTOR,
    )  # fmt: skip
    state, fell_back = domoticz_api.load_state()
    assert fell_back is False and "30" not in state.level_maps.get("H1", {})
    assert domoticz_api.recover_from_devices(domoticz.Devices, state) == []
    assert state.level_maps["H1"]["30"] == ["A", "B", "C"]


def test_recover_keeps_a_persisted_level_map(domoticz):
    make_unit("H1", 30, {"HCKey": "K", "HCKind": "selector", "HCKeys": "|X|Y"}, SELECTOR)
    state = persistence.State(level_maps={"H1": {"30": ["A", "B"]}})
    assert domoticz_api.recover_from_devices(domoticz.Devices, state) == []
    assert state.level_maps["H1"]["30"] == ["A", "B"]


def test_state_saver_cadence(domoticz):
    state = persistence.State()
    saver = domoticz_api.StateSaver(state)
    assert saver.on_heartbeat() is False  # matches the loaded state
    assert saver.on_heartbeat() is False  # not dirty
    state.next_unit["D1"] = 30
    assert saver.on_heartbeat() is True
    state.tokens = persistence.Tokens("tok-a", "tok-r", 1.0, "s", "h")
    assert saver.after_token_change() is True  # immediate
    assert saver.on_heartbeat() is False  # already written this change
    state.auth_state = "AUTHORIZED"
    assert saver.on_stop() is True
    assert saver.on_stop() is False
    loaded, _ = domoticz_api.load_state()
    assert loaded.auth_state == "AUTHORIZED" and loaded.next_unit == {"D1": 30}


def test_saving_logs_nothing_and_never_leaks_tokens(domoticz):
    state = persistence.State(tokens=persistence.Tokens("secret-a", "secret-r", 1.0, "s", "h"))
    domoticz_api.StateSaver(state).after_token_change()
    assert domoticz._log == []


def test_log_levels_and_settings(domoticz):
    domoticz_api.log("error", "boom")
    domoticz_api.log("other", "plain")
    domoticz_api.heartbeat(30)
    assert domoticz._log == [("error", "boom"), ("log", "plain")]
    assert domoticz._heartbeat == 30


def test_debug_levels_map_to_domoticz_masks():
    # Basic is the plugin's own debug lines (mask 2), Verbose adds the framework's (mask 1).
    for level, mask in ((0, 0), (1, 2), (2, 1)):
        domoticz_api.debugging(level)
        assert Domoticz._debugging == mask, level


def test_source_never_deletes_units_or_updates_typename():
    tree = ast.parse(Path(domoticz_api.__file__).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        assert node.func.attr != "Delete"
        if node.func.attr == "Update":
            assert all(kw.arg != "TypeName" for kw in node.keywords)
    text = Path(domoticz_api.__file__).read_text()
    assert ".Delete(" not in text


def test_failed_create_does_not_abort_the_batch(domoticz, monkeypatch):
    real_create = Domoticz.Unit.Create

    def create(self):
        if self.Unit == 3:
            return  # Domoticz refused: the unit never appears
        real_create(self)

    monkeypatch.setattr(Domoticz.Unit, "Create", create)
    state = persistence.State()
    created = domoticz_api.apply_updates(domoticz.Devices, [up(1), up(3), up(5)], state)
    assert created == 2
    assert list(domoticz.Devices["D1"].Units) == [1, 5]
    assert "D1:3" not in state.auto_names
    errors = [m for lvl, m in domoticz._log if lvl == "error"]
    assert errors == ["Could not create appliance unit 3"]


def test_failed_create_of_the_account_device_names_it(domoticz, monkeypatch):
    monkeypatch.setattr(Domoticz.Unit, "Create", lambda self: None)
    domoticz_api.apply_updates(domoticz.Devices, [up(1, dev="account")], persistence.State())
    assert domoticz._log == [("error", 'Could not create "account" unit 1')]


def test_non_mapping_options_count_as_empty(domoticz):
    state = persistence.State()
    planned = {"LevelNames": "a|b"}
    for bad in ("garbage", None):
        domoticz_api.apply_updates(
            domoticz.Devices, [up(20, "1", 1, type_name=SELECTOR, options=planned)], state
        )
        unit = domoticz.Devices["D1"].Units[20]
        unit.Options = bad
        domoticz_api.apply_updates(
            domoticz.Devices, [up(20, "1", 1, type_name=SELECTOR, options=planned)], state
        )
        assert unit.stored["Options"] == planned
    make_unit("H2", 30, {"HCKind": "selector"}, SELECTOR).Options = "garbage"
    assert ("H2", 30) in domoticz_api.recover_from_devices(domoticz.Devices, state)


def test_empty_planned_options_leave_user_options_alone(domoticz):
    state = persistence.State()
    domoticz_api.apply_updates(domoticz.Devices, [up(1)], state)
    unit = domoticz.Devices["D1"].Units[1]
    unit.Options = {"Custom": "mine"}
    unit.stored["Options"] = dict(unit.Options)
    domoticz_api.apply_updates(domoticz.Devices, [up(1)], state)
    assert unit.stored["Options"] == {"Custom": "mine"}


def test_unit_vanishing_mid_batch_is_skipped(domoticz):
    state = persistence.State()
    domoticz_api.apply_updates(domoticz.Devices, [up(1), up(2)], state)
    del domoticz.Devices["D1"].Units[1]
    assert domoticz_api.apply_updates(domoticz.Devices, [up(1, "n"), up(2, "n")], state) == 1
    assert domoticz.Devices["D1"].Units[2].stored["sValue"] == "n"


def test_blocked_selector_found_after_a_user_edit_dropped_hckind(domoticz):
    make_unit("H1", 30, {"LevelNames": "-|a"}, SELECTOR, Switchtype=18)
    make_unit("H1", 31, {}, "Text")
    make_unit("H1", 32, {}, "Text")
    state = persistence.State(unit_kinds={"H1": {"32": "selector"}})
    blocked = domoticz_api.recover_from_devices(domoticz.Devices, state)
    assert blocked == [("H1", 30), ("H1", 32)]


def test_state_saver_does_not_rewrite_loaded_state_on_first_heartbeat(domoticz):
    state = persistence.State(next_unit={"D1": 25})
    saver = domoticz_api.StateSaver(state)
    assert saver.on_heartbeat() is False
    assert domoticz._config == {}


def test_state_saver_survives_a_failing_configuration(domoticz, monkeypatch):
    state = persistence.State()
    saver = domoticz_api.StateSaver(state)
    state.next_unit["D1"] = 30

    def boom(config=None):
        raise RuntimeError("db locked")

    monkeypatch.setattr(domoticz, "Configuration", boom)
    assert saver.on_heartbeat() is False
    assert saver.on_stop() is False
    assert domoticz._log[0] == ("error", "State could not be saved (RuntimeError); will retry")
    monkeypatch.undo()
    assert saver.on_heartbeat() is True


def test_image_is_set_at_creation_only(domoticz):
    state = persistence.State()
    domoticz_api.apply_updates(domoticz.Devices, [up(20, type_name="Switch", image=28)], state)
    unit = domoticz.Devices["D1"].Units[20]
    assert unit.stored["Image"] == 28
    unit.Image = 9  # the user picks another icon
    unit.Update(Log=False, UpdateProperties=True)
    later = up(20, "y", type_name="Switch", image=15, name="Renamed")
    domoticz_api.apply_updates(domoticz.Devices, [later], state)
    assert (unit.Image, unit.stored["Image"]) == (9, 9)


def test_reset_only_update_never_creates_but_writes_an_existing_unit(domoticz):
    state = persistence.State()
    assert domoticz_api.apply_updates(domoticz.Devices, [up(4, "0", create=False)], state) == 0
    assert "D1" not in domoticz.Devices
    domoticz_api.apply_updates(domoticz.Devices, [up(4, "69")], state)
    domoticz_api.apply_updates(domoticz.Devices, [up(4, "0", create=False)], state)
    assert domoticz.Devices["D1"].Units[4].sValue == "0"


def test_status_code_selector_is_never_blocked(domoticz):
    # Its levels are fixed in code, so it carries no HCKeys and needs none.
    options = {"LevelNames": "-|Inactive", "SelectorStyle": "1"}
    make_unit("H1", planner.UNIT_STATUS_CODE, options, SELECTOR, Switchtype=18)
    state = persistence.State(unit_kinds={"H1": {str(planner.UNIT_STATUS_CODE): "selector"}})
    assert domoticz_api.recover_from_devices(domoticz.Devices, state) == []


def test_program_selector_levels_are_recovered_from_hckeys(domoticz):
    unit = planner.UNIT_PROGRAM_SELECT
    make_unit("H1", unit, {"LevelNames": "-|A|B", "HCKeys": "|P.A|P.B"}, SELECTOR, Switchtype=18)
    make_unit("H2", unit, {"LevelNames": "-|A"}, SELECTOR, Switchtype=18)
    state = persistence.State()
    blocked = domoticz_api.recover_from_devices(domoticz.Devices, state)
    assert state.level_maps["H1"][str(unit)] == ["P.A", "P.B"]
    assert blocked == [("H2", unit)]


def test_code_defined_control_selectors_are_never_blocked(domoticz):
    for unit in (planner.UNIT_ACTIONS, planner.UNIT_DELAYED_START):
        make_unit("H1", unit, {"LevelNames": "-|Start"}, SELECTOR, Switchtype=18)
    assert domoticz_api.recover_from_devices(domoticz.Devices, persistence.State()) == []


def test_unit_name_falls_back_to_the_number(domoticz):
    make_unit("D1", 9, {})
    assert domoticz_api.unit_name(domoticz.Devices, "D1", 9) == "u9"
    assert domoticz_api.unit_name(domoticz.Devices, "D1", 8) == "unit 8"
    assert domoticz_api.unit_name(domoticz.Devices, "missing", 8) == "unit 8"


def test_description_is_set_at_creation_only(domoticz):
    state = persistence.State()
    domoticz_api.apply_updates(domoticz.Devices, [up(20, description="Shows the state.")], state)
    unit = domoticz.Devices["D1"].Units[20]
    assert unit.stored["Description"] == "Shows the state."
    unit.Description = "My own note"  # the user edits it
    unit.Update(Log=False, UpdateProperties=True)
    domoticz_api.apply_updates(
        domoticz.Devices, [up(20, "y", description="Other text", name="Renamed")], state
    )
    assert unit.stored["Description"] == "My own note"
