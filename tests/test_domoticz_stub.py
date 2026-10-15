import DomoticzEx as Domoticz


def test_unit_create_registers_device_and_persists_values(domoticz):
    unit = Domoticz.Unit(Name="Door", DeviceID="dev1", Unit=3, TypeName="Switch", Switchtype=11)
    unit.nValue = 1
    unit.sValue = "On"
    unit.Create()
    assert domoticz.Devices["dev1"].Units[3] is unit
    assert unit.stored["sValue"] == "On"
    assert unit.SwitchType == 11


def test_update_persists_options_only_with_flag(domoticz):
    unit = Domoticz.Unit(
        Name="Sel", DeviceID="d", Unit=20, TypeName="Selector Switch", Options={"LevelNames": "a|b"}
    )
    unit.Create()
    unit.Options = {"LevelNames": "a|b|c"}
    unit.Update(Log=False)
    assert unit.stored["Options"] == {"LevelNames": "a|b"}
    unit.Update(Log=False, UpdateOptions=True)
    assert unit.stored["Options"] == {"LevelNames": "a|b|c"}


def test_used_persists_only_with_properties_flag(domoticz):
    unit = Domoticz.Unit(Name="X", DeviceID="d", Unit=1, TypeName="Text", Used=1)
    unit.Create()
    unit.Used = 0
    unit.Update(Log=False)
    assert unit.stored["Used"] == 1
    unit.Update(Log=False, UpdateProperties=True)
    assert unit.stored["Used"] == 0


def test_configuration_round_trip(domoticz):
    Domoticz.Configuration({"state": "x"})
    assert Domoticz.Configuration() == {"state": "x"}


def test_unit_has_no_delete():
    assert not hasattr(Domoticz.Unit, "Delete")
