"""Minimal in-memory fake of the DomoticzEx plugin API for unit tests.

It records what Domoticz would PERSIST, not just what was assigned: the core writes Name,
Description, Image and Used only under Update(UpdateProperties=True) and Options only under
UpdateOptions=True (hardware/plugins/PythonObjectEx.cpp, CUnitEx_update). A stub that ignored
the difference would pass code that can never work against real Domoticz. Deliberately has no
Delete(): the plugin must never call it.
"""

import sys
import types


class Unit:
    def __init__(
        self,
        Name="",
        DeviceID="",
        Unit=0,
        TypeName="",
        Options=None,
        Used=0,
        Image=0,
        Switchtype=0,
        Description="",
        **_kw,
    ):
        self.Name = Name
        self.DeviceID = DeviceID
        self.Unit = Unit
        self.TypeName = TypeName
        self.Type = 0
        self.SubType = 0
        self.Options = dict(Options or {})
        self.Used = Used
        self.Image = Image
        self.SwitchType = Switchtype
        self.Description = Description
        self.nValue = 0
        self.sValue = ""
        self.LastUpdate = ""
        self.stored: dict = {}
        self.updates: list[dict] = []

    def _snapshot(self) -> dict:
        return {
            "Name": self.Name,
            "Description": self.Description,
            "Image": self.Image,
            "Used": self.Used,
            "Options": dict(self.Options),
            "nValue": self.nValue,
            "sValue": self.sValue,
        }

    def Create(self):
        dev = Devices.setdefault(self.DeviceID, _FakeDevice(self.DeviceID))
        dev.Units[self.Unit] = self
        self.stored = self._snapshot()

    def Update(self, **kw):
        self.updates.append(kw)
        self.stored["nValue"] = self.nValue
        self.stored["sValue"] = self.sValue
        if kw.get("UpdateProperties"):
            self.stored["Name"] = self.Name
            self.stored["Description"] = self.Description
            self.stored["Image"] = self.Image
            self.stored["Used"] = self.Used
        if kw.get("UpdateOptions"):
            self.stored["Options"] = dict(self.Options)


class _FakeDevice:
    def __init__(self, device_id):
        self.DeviceID = device_id
        self.Units: dict[int, Unit] = {}


Devices: dict[str, _FakeDevice] = {}
_module = types.ModuleType("DomoticzEx")
_module._log = []
_module._heartbeat = None
_module._debugging = 0
_module._config = {}
_module.Unit = Unit
_module.Devices = Devices


def _logger(level):
    def log(msg):
        _module._log.append((level, str(msg)))

    return log


_module.Log = _logger("log")
_module.Debug = _logger("debug")
_module.Error = _logger("error")
_module.Status = _logger("status")
_module.Heartbeat = lambda seconds: setattr(_module, "_heartbeat", seconds)
_module.Debugging = lambda value: setattr(_module, "_debugging", value)


def _configuration(config=None):
    if config is not None:
        _module._config = dict(config)
    return dict(_module._config)


_module.Configuration = _configuration


def install():
    """Install the fake module under both names and reset its state."""
    Devices.clear()
    _module._log.clear()
    _module._heartbeat = None
    _module._debugging = 0
    _module._config = {}
    sys.modules["Domoticz"] = _module
    sys.modules["DomoticzEx"] = _module
    return _module
