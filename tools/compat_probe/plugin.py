# pyright: reportMissingImports=false, reportUndefinedVariable=false
"""
<plugin key="hccompat" name="Home Connect compatibility probe" author="Rouzax" version="0.0.1">
    <description><p>Development probe. Creates three test devices and logs what Domoticz stored.</p></description>
    <params/>
</plugin>
"""

import DomoticzEx as Domoticz

# Fully fake ids: a real appliance model code must not appear in committed code.
# LONG_ID is 30 characters, the length of a Siemens haId, which is what the DeviceID must hold intact.
LONG_ID = "SIEMENS-" + "0" * 22
# 40 characters, past the haId length, to find where Domoticz starts truncating DeviceIDs.
LONGER_ID = "X" * 40
# 16 levels, more than 11, so the top level is 150 and a level above 100 exists for the scene level cap check.
LEVELS = 16
# HCKeys is a custom option key that Domoticz does not define; whether a user edit in the Setup UI keeps it is what the probe tests.
KEYS = "|".join(f"Cooking.Oven.Program.HeatingMode.P{i:02d}" for i in range(LEVELS))


def onStart():
    Domoticz.Heartbeat(10)
    if LONG_ID not in Devices:
        unit = Domoticz.Unit(
            Name="Probe long id", DeviceID=LONG_ID, Unit=1, TypeName="Text", Used=1
        )
        unit.Create()
    if LONGER_ID not in Devices:
        unit = Domoticz.Unit(
            Name="Probe 40 id", DeviceID=LONGER_ID, Unit=1, TypeName="Text", Used=1
        )
        unit.Create()
    if "probe" not in Devices:
        names = "Not available|" + "|".join(f"Program {i + 1}" for i in range(LEVELS))
        unit = Domoticz.Unit(
            Name="Probe selector",
            DeviceID="probe",
            Unit=1,
            TypeName="Selector Switch",
            Switchtype=18,
            Used=1,
            Options={
                "LevelNames": names,
                "LevelActions": "|" * LEVELS,
                "LevelOffHidden": "false",
                "SelectorStyle": "1",
                "HCKeys": "|" + KEYS,
            },
        )
        unit.Create()


def onHeartbeat():
    for dev_id, dev in Devices.items():
        for unit_no, unit in dev.Units.items():
            Domoticz.Status(
                f"probe device id={dev_id!r} len={len(dev_id)} unit={unit_no} "
                f"svalue={unit.sValue!r} hckeys_present={'HCKeys' in (unit.Options or {})}"
            )


def onCommand(DeviceID, Unit, Command, Level, Color):
    Domoticz.Status(
        f"probe command device={DeviceID!r} unit={Unit} command={Command!r} level={Level!r}"
    )
