# pyright: reportMissingImports=false, reportMissingModuleSource=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Thin adapter over the DomoticzEx device API (spec 4.2).

Together with plugin.py the only module importing Domoticz. It never calls
Update with a TypeName argument, which converts a live device in place and leaves its history read
under the new meaning (core #6981), and never deletes a unit, which in a plugin skips the core's
cleanup and lets a later device inherit the old one's history (core #6982). An AST test guards
both.
"""

import DomoticzEx as Domoticz

import persistence
import planner
import text

# Device types whose value is a number: an empty planned value means "not reported", and nothing
# is written rather than a fabricated zero (spec 6.4).
# Meters count too: a kWh device by name, a counter by its numeric type_code.
_NUMERIC_TYPES = frozenset({"Set Point", "Temperature", "Percentage", "Custom", "kWh"})
_SELECTOR_SWITCHTYPE = 18
_LOGGERS = {"error": "Error", "status": "Status", "debug": "Debug", "log": "Log"}
# Debug level to Domoticz.Debugging() mask: 2 shows only this plugin's own Debug lines, 1 adds
# the framework's internals (spec 5.1).
_DEBUG_MASKS = {0: 0, 1: 2, 2: 1}


def load_state() -> tuple[persistence.State, bool]:
    raw = Domoticz.Configuration().get(persistence.STATE_KEY, "")
    return persistence.loads(raw if isinstance(raw, str) else "")


def save_state(state: persistence.State) -> None:
    # The in-memory state is authoritative after onStart, so there is no read before the write
    # (spec 4.4); the plugin owns the whole Configuration dictionary.
    Domoticz.Configuration({persistence.STATE_KEY: persistence.dumps(state)})


class StateSaver:
    """Spec 4.4 cadence: save at once after a token change, otherwise once per heartbeat and
    only when the serialized state differs from what was last written, and in onStop.

    Comparing the serialized form replaces dirty tracking, so no caller can forget to mark a
    change; Configuration() rewrites the whole column, so skipping unchanged writes is what
    bounds the load.
    """

    def __init__(self, state: persistence.State) -> None:
        self._state = state
        # Seeded from the loaded state so the first heartbeat does not rewrite what is stored.
        self._written: str | None = persistence.dumps(state)

    def _write(self) -> bool:
        """Returns True when a write happened. A failing Configuration() is reported and
        swallowed: persistence is best effort and must never raise into a Domoticz callback;
        the next call retries because _written is unchanged."""
        serialized = persistence.dumps(self._state)
        if serialized == self._written:
            return False
        try:
            save_state(self._state)
        except Exception as exc:  # the framework call can raise anything
            log("error", f"State could not be saved ({type(exc).__name__}); will retry")
            return False
        self._written = serialized
        return True

    def after_token_change(self) -> bool:
        return self._write()

    def on_heartbeat(self) -> bool:
        return self._write()

    def on_stop(self) -> bool:
        return self._write()


def _skips_value(update: planner.DeviceUpdate) -> bool:
    """True when nothing may be written as this unit's value.

    Either Home Connect reported none (has_value False), or the value is an empty string for a
    numeric type.
    """
    if not update.has_value:
        return True
    numeric = update.type_code is not None or update.type_name in _NUMERIC_TYPES
    return update.svalue == "" and numeric


def _changed(unit, update: planner.DeviceUpdate) -> bool:
    if _skips_value(update):
        return False
    if unit.nValue != update.nvalue:
        return True
    if str(unit.sValue) == update.svalue:
        return False
    if update.deadband:
        try:
            return abs(float(unit.sValue) - float(update.svalue)) >= update.deadband
        except (TypeError, ValueError):
            return True
    return True


def _options(unit) -> dict:
    """The unit's Options as a dict; anything else (None, a string) counts as empty."""
    options = getattr(unit, "Options", None)
    return dict(options) if isinstance(options, dict) else {}


def _where(update: planner.DeviceUpdate) -> str:
    # Appliance device ids are haIds and are never logged.
    device = (
        text.log_value(update.device_id)
        if update.device_id == planner.ACCOUNT_DEVICE
        else "appliance"
    )
    return f"{device} unit {update.unit}"


def _create(devices, update: planner.DeviceUpdate):
    """Create the unit; None when Domoticz did not register it."""
    # A type without a Python TypeName (an RFXMeter counter) is created by number instead.
    if update.type_code is not None:
        type_args = {"Type": update.type_code[0], "Subtype": update.type_code[1]}
    else:
        type_args = {"TypeName": update.type_name}
    new_unit = Domoticz.Unit(
        Name=update.name,
        DeviceID=update.device_id,
        Unit=update.unit,
        **type_args,
        Options=dict(update.options),
        Switchtype=update.switchtype,
        Used=update.used,
        # Creation only: a later update never sends Image, so an icon the user picks is kept.
        Image=update.image,
        # Creation only as well: a description the user edits is kept.
        Description=update.description,
    )
    new_unit.Create()
    dev = devices.get(update.device_id)
    unit = dev.Units.get(update.unit) if dev is not None else None
    if unit is None:
        return None
    if not _skips_value(update):
        unit.nValue = update.nvalue
        unit.sValue = update.svalue
        unit.Update(Log=False)
    return unit


def apply_updates(devices, updates, state: persistence.State, *, allow_create: bool = True) -> int:
    """Write the planned units; returns how many were created."""
    created = 0
    # Ascending order: Domoticz lists devices in creation order.
    for update in sorted(updates, key=lambda u: (u.device_id, u.unit)):
        dev = devices.get(update.device_id)
        unit = dev.Units.get(update.unit) if dev is not None else None
        name_key = f"{update.device_id}:{update.unit}"
        if unit is None:
            if not allow_create or not update.create:
                continue
            if _create(devices, update) is None:
                log("error", f"Could not create {_where(update)}")
                continue
            state.auto_names[name_key] = update.name
            created += 1
            continue
        # An update without options (no selector, or nothing planned) leaves the unit's Options
        # alone, so a user's edit to a unit the plugin has no opinion on survives.
        # The plugin owns the whole Options dict, level names and the HC* keys included: an edit
        # in the selector dialog drops the custom keys, and this restores them.
        if update.options and _options(unit) != update.options:
            unit.Options = dict(update.options)
            unit.Update(Log=False, UpdateOptions=True)
        if _changed(unit, update):
            unit.nValue = update.nvalue
            unit.sValue = update.svalue
            unit.Update(Log=False)
        # Rename only while the name is still the one the plugin gave it: a user's own name wins.
        if unit.Name != update.name and unit.Name == state.auto_names.get(name_key):
            unit.Name = update.name
            unit.Update(Log=False, UpdateProperties=True)
            state.auto_names[name_key] = update.name
    return created


def unit_name(devices, device_id: str, number: int) -> str:
    """The unit's name as the user sees it, for a log line."""
    dev = devices.get(device_id)
    unit = dev.Units.get(number) if dev is not None else None
    return str(unit.Name) if unit is not None else f"unit {number}"


def set_used(devices, device_id: str, units, used: bool) -> None:
    dev = devices.get(device_id)
    if dev is None:
        return
    for number in units:
        unit = dev.Units.get(number)
        if unit is not None and bool(unit.Used) != used:
            unit.Used = 1 if used else 0
            unit.Update(Log=False, UpdateProperties=True)


def _is_selector(state: persistence.State, dev_id: str, number: int, unit, kind) -> bool:
    """A user edit drops HCKind, so the persisted kind and the unit's own selector Switchtype
    also count: such a unit must still be reported blocked when its level map is lost."""
    if kind == "selector" or state.unit_kinds.get(dev_id, {}).get(str(number)) == "selector":
        return True
    if getattr(unit, "SwitchType", 0) == _SELECTOR_SWITCHTYPE:
        return True
    return getattr(unit, "TypeName", "") == "Selector Switch"


def recover_from_devices(devices, state: persistence.State) -> list[tuple[str, int]]:
    """Reconcile the state with the existing devices; run on every start (spec 7.3 step 1).

    Raises next_unit past the highest existing unit and fills gaps in the critical maps from the
    devices' own HCKey/HCKind/HCKeys options. It only fills what is missing (setdefault) and
    never guesses a key from a level name, so a restored backup can never make a stored scene
    level mean another program (spec 4.4). Returns the selectors whose level keys could not be
    recovered: they keep their device and history, but commands to them are refused until the
    user restores a backup or deletes the device.
    """
    blocked: list[tuple[str, int]] = []
    for dev_id, dev in devices.items():
        if dev_id == planner.ACCOUNT_DEVICE or not dev.Units:
            continue
        highest = max(dev.Units)
        floor = state.next_unit.get(dev_id, persistence.FIRST_DYNAMIC_UNIT)
        state.next_unit[dev_id] = max(floor, highest + 1)
        for number, unit in dev.Units.items():
            options = _options(unit)
            key, kind = options.get("HCKey"), options.get("HCKind")
            if key:
                state.unit_alloc.setdefault(dev_id, {}).setdefault(key, number)
            if kind:
                state.unit_kinds.setdefault(dev_id, {}).setdefault(str(number), kind)
            # A fixed unit's levels are defined in code (actions, status code, delayed start),
            # so there is no level map to lose; the program selector's come from Home Connect.
            fixed = number < persistence.FIRST_DYNAMIC_UNIT
            if fixed and number not in planner.DATA_DRIVEN_FIXED_UNITS:
                continue
            if not _is_selector(state, dev_id, number, unit, kind):
                continue
            levels = state.level_maps.setdefault(dev_id, {})
            if str(number) in levels:
                continue
            keys = options.get("HCKeys")
            if keys:
                levels[str(number)] = [k for k in keys.split("|")[1:] if k]
            else:
                blocked.append((dev_id, number))
    return blocked


def log(level: str, message: str) -> None:
    getattr(Domoticz, _LOGGERS.get(level, "Log"))(message)


def debugging(level: int) -> None:
    Domoticz.Debugging(_DEBUG_MASKS.get(level, 0))


def heartbeat(seconds: int) -> None:
    Domoticz.Heartbeat(seconds)
