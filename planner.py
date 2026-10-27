"""Model to Domoticz device updates (spec 6). Pure; allocates units and levels in State.

A Domoticz device's type can never change after creation (spec 6.4), so the kind chosen for a
unit is recorded in state.unit_kinds and reused for ever; that is also why option and setting
devices wait until their writability is known instead of guessing.
"""

import functools
import math
import time
from collections import Counter
from collections.abc import Container, Iterable, Mapping
from dataclasses import dataclass, field, replace

import catalog
import model
import persistence
import text

UNIT_STATE, UNIT_POWER, UNIT_DOOR, UNIT_PROGRESS, UNIT_TIME, UNIT_PROGRAM, UNIT_REMOTE = range(1, 8)
# Control units (spec 6.2): the program selector, the actions selector and the acknowledge
# button. A unit number is permanent once created.
UNIT_PROGRAM_SELECT, UNIT_ACTIONS, UNIT_ACK = 8, 9, 10
# Read-only selector for scripts: the operation state as a level that never changes meaning.
UNIT_STATUS_CODE = 11
# Delayed start as a clock-time selector (spec 6.3), on appliances that offer a delay.
UNIT_DELAYED_START = 12
# Fixed units whose levels come from Home Connect data rather than from code: their key list
# travels in HCKeys, so a lost snapshot is recovered from the device as for a dynamic selector.
DATA_DRIVEN_FIXED_UNITS = (UNIT_PROGRAM_SELECT,)
# Units that exist only while AllowControl is on, hidden and shown once per transition (spec
# 6.4). The program selector is not one of them: it also tells scripts, as a level that never
# changes meaning, which program is selected or running.
CONTROL_ONLY_UNITS = (UNIT_ACTIONS, UNIT_ACK, UNIT_DELAYED_START)
# Planned whatever AllowControl says (scripts may read them), but only shown while it is on.
CONTROL_SHOWN_UNITS = (UNIT_REMOTE,)
# Action levels are a public contract for scenes and scripts, the same on every appliance:
# never renumber or reuse one. Level 0 is "-", to which the selector always returns, so the
# same action chosen twice runs twice. The door actions come last: an appliance without them
# shows the levels before them only, which renumbers nothing.
ACTION_LEVELS = {
    "start": 10,
    "stop": 20,
    "pause": 30,
    "resume": 40,
    "open_door": 50,
    "partly_open_door": 60,
}
_DOOR_ACTIONS = ("open_door", "partly_open_door")
_DOOR_COMMANDS = frozenset({catalog.OPEN_DOOR, catalog.PARTLY_OPEN_DOOR})
# Delayed-start levels are local clock times this many minutes apart, 00:00 at level 10. A
# level's time is a public contract too: changing the step moves every stored timer or scene
# level to another time.
DELAY_STEP_MIN = 30
# Status code levels are a public contract for scripts: never renumber or reuse one. Level 0
# (unknown, hidden) is never written.
STATUS_CODE_STATES = (
    "Inactive",
    "Ready",
    "DelayedStart",
    "Run",
    "Pause",
    "ActionRequired",
    "Finished",
    "Aborting",
    "Error",
)
STATUS_CODE_LEVELS = {name: 10 * (i + 1) for i, name in enumerate(STATUS_CODE_STATES)}
STATUS_CODE_OFFLINE = 100
ACCOUNT_DEVICE = "account"
ACCOUNT_TEXT, ACCOUNT_ACTIONS, ACCOUNT_CALLS = 1, 2, 3
MAX_UNIT = 255

_LABEL_MAX = 60
_INTERNAL_KEY_PARTS = (".Status.Program.History.", ".Status.Program.Details.")
_NAME_MAX = 100
_LEVEL_NAME_MAX = 40
_TEXT_MAX = 200
_UNIT_TEXT_MAX = 20
_MINUTES_OPTIONS = {"Custom": "1;min"}
# Home Connect sends min and max but usually no stepsize, and Domoticz then steps a setpoint by
# 0.5. Whole units suit temperatures, counts and minutes. A stepsize Home Connect does send
# always wins.
_DEFAULT_STEP = "1"
# A time Home Connect counts in seconds (a cooking Duration) shows in minutes, as the Home Connect
# app does; a typed 0.5 still gives 30 s. control converts a commanded value back (spec 6.3).
SECONDS_PER_MINUTE = 60
# Lifetime totals shown as Domoticz meters (catalog.meter).
_METER_KINDS = frozenset({"energy_total", "counter_total"})
_NUMERIC_KINDS = (
    frozenset({"setpoint", "temperature", "percentage", "custom", "minutes"}) | _METER_KINDS
)
# No real reading comes near this; a larger number (still finite after json.loads) would overflow
# time.localtime() in a finish time or fill a device with hundreds of digits.
_NUMBER_LIMIT = 1e12

# The kind of each fixed unit, for what the kind decides beyond the type (the icon).
_FIXED_KINDS = {
    UNIT_STATE: "alert",
    UNIT_POWER: "switch",
    UNIT_DOOR: "door",
    UNIT_PROGRESS: "percentage",
    UNIT_TIME: "minutes",
    UNIT_PROGRAM: "text",
    UNIT_REMOTE: "alert",
    UNIT_STATUS_CODE: "selector",
    UNIT_PROGRAM_SELECT: "selector",
    UNIT_ACTIONS: "selector",
    UNIT_ACK: "push",
    UNIT_DELAYED_START: "selector",
}

# What each fixed unit's description explains, and which of them send commands to the appliance.
_FIXED_DESCRIPTIONS = {
    UNIT_STATE: "state",
    UNIT_POWER: "power",
    UNIT_DOOR: "door",
    UNIT_PROGRESS: "progress",
    UNIT_TIME: "remaining",
    UNIT_PROGRAM: "program",
    UNIT_REMOTE: "remote",
    UNIT_STATUS_CODE: "status_code",
    UNIT_PROGRAM_SELECT: "program_select",
    UNIT_ACK: "acknowledge",
}
_COMMAND_UNITS = frozenset(
    {UNIT_POWER, UNIT_PROGRAM_SELECT, UNIT_ACTIONS, UNIT_ACK, UNIT_DELAYED_START}
)
_WRITABLE_KINDS = frozenset({"switch", "selector", "setpoint"})

_KIND_TYPES = {
    "alert": ("Alert", 0),
    "switch": ("Switch", 0),
    "door": ("Switch", 11),
    "percentage": ("Percentage", 0),
    "minutes": ("Custom", 0),
    "text": ("Text", 0),
    "selector": ("Selector Switch", 18),
    "setpoint": ("Set Point", 0),
    "temperature": ("Temperature", 0),
    "custom": ("Custom", 0),
    "push": ("Push On", 9),
    "energy_total": ("kWh", 0),
    "counter_total": ("", 3),
}
# Numeric Type and Subtype for kinds whose device type has no TypeName in the Python plugin
# layer: an RFXMeter counter.
_KIND_CODES = {"counter_total": (113, 0)}


@dataclass(frozen=True)
class DeviceUpdate:
    """One unit as the plan wants it.

    has_value False means "create the unit if it is missing, but write no value": Home Connect
    has not reported one (an option another program offers, spec 6.3), and only reported values
    are written (spec 6.4). The writer then ignores nvalue and svalue, never updates an existing
    unit's value from it, and still applies name, type, options and Used at creation and the
    Options rewrite rule afterwards. Selectors never use it: level 0 "Not available" is their
    real state.

    create False means "update the unit only if it already exists": a reset of a run card is
    never a reason to create one, so an appliance without programs never gains them. The writer
    skips such an update when the unit is missing.
    """

    device_id: str
    unit: int
    name: str
    type_name: str
    nvalue: int
    svalue: str
    options: dict = field(default_factory=dict)
    switchtype: int = 0
    used: int = 1
    deadband: float = 0.0
    has_value: bool = True
    image: int = 0  # built-in icon id, applied at creation only; 0 is the Domoticz default
    create: bool = True
    description: str = ""  # applied at creation only, so a user's own text survives
    # Numeric (Type, Subtype), used at creation instead of type_name when set.
    type_code: tuple[int, int] | None = None


@dataclass(frozen=True)
class AccountView:
    state: str
    user_code: str = ""
    link: str = ""
    valid_until: str = ""
    message: str = ""
    calls: int = 0


def device_id(ha_id: str) -> str:
    """The readable haId: Domoticz stores and matches 30- and 40-character DeviceIDs intact
    (spec 6.1, 12)."""
    return ha_id


def clock(epoch: float) -> str:
    """Local wall-clock time as shown to the user in device text."""
    return time.strftime("%H:%M", time.localtime(epoch))


def delay_times() -> list[int]:
    """Minutes after midnight of each delayed-start level, level 10 first."""
    return list(range(0, 24 * 60, DELAY_STEP_MIN))


def delay_level(minutes: int | None) -> int:
    times = delay_times()
    return 10 * (times.index(minutes) + 1) if minutes in times else 0


def delay_minutes(level: int) -> int | None:
    times = delay_times()
    index = level // 10 - 1
    return times[index] if level % 10 == 0 and 0 <= index < len(times) else None


def clock_text(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def delay_key(app: model.Appliance) -> str | None:
    """The delayed-start option the appliance offers, or None. Laundry offers FinishInRelative
    ("ready at"), ovens and dishwashers StartInRelative ("start at") (spec 3.2a)."""
    return pick_delay_key(set(_offered_options(app)) | set(app.options))


def pick_delay_key(keys: Iterable[object]) -> str | None:
    """The delayed-start key among `keys`; FinishInRelative wins when both are present."""
    found = set(keys)
    return next((k for k in (catalog.FINISH_IN, catalog.START_IN) if k in found), None)


def display_names(
    appliances: Iterable[model.Appliance], warnings: list[str] | None = None
) -> dict[str, str]:
    """haId to appliance label. Duplicate names fall back to name, brand and type (spec 6.4),
    numbered if even that collides; one warning per duplicated name is appended to `warnings`
    for the caller to log, since this module cannot. Sorted by haId so a label never moves
    between appliances when the listing order changes."""
    apps = sorted(appliances, key=lambda a: a.ha_id)
    counts = Counter(a.name for a in apps)
    names = {
        a.ha_id: a.name
        if a.name and counts[a.name] == 1
        else text.clean_label(" ".join(p for p in (a.name, a.brand, a.type) if p), _LABEL_MAX)
        for a in apps
    }
    clashes = Counter(names.values())
    numbered: Counter[str] = Counter()
    for a in apps:
        base = names[a.ha_id]
        if clashes[base] > 1:
            numbered[base] += 1
            # Room for the suffix, so the label cap in _name never cuts it off.
            names[a.ha_id] = f"{text.clean_label(base, _LABEL_MAX - 4)} {numbered[base]}"
    if warnings is not None:
        for name in sorted(n for n, c in counts.items() if n and c > 1):
            warnings.append(
                f"duplicate_appliance_name name={text.log_value(name)} count={counts[name]}"
                " fallback=brand_and_type"
            )
    return names


def _name(label: str, item: str) -> str:
    return text.clean_label(f"{text.clean_label(label, _LABEL_MAX)} {item}", _NAME_MAX)


def _update(
    dev: str, unit: int, label: str, item: str, kind: str, nvalue: int, svalue: str, **kw
) -> DeviceUpdate:
    type_name, switchtype = _KIND_TYPES[kind]
    return DeviceUpdate(
        dev,
        unit,
        _name(label, item),
        type_name,
        nvalue,
        svalue,
        switchtype=switchtype,
        type_code=_KIND_CODES.get(kind),
        **kw,
    )


def _binary(dev: str, unit: int, label: str, item: str, kind: str, on: bool) -> DeviceUpdate:
    return _update(dev, unit, label, item, kind, 1 if on else 0, "On" if on else "Off")


def _allocate(state: persistence.State, dev: str, key: str) -> int | None:
    units = state.unit_alloc.setdefault(dev, {})
    if key in units:
        return units[key]
    unit = max(
        state.next_unit.get(dev, persistence.FIRST_DYNAMIC_UNIT), persistence.FIRST_DYNAMIC_UNIT
    )
    if unit > MAX_UNIT:
        return None
    units[key] = unit
    state.next_unit[dev] = unit + 1
    return unit


def _kind(state: persistence.State, dev: str, unit: int, wanted: str) -> str:
    kinds = state.unit_kinds.setdefault(dev, {})
    return kinds.setdefault(str(unit), wanted)


def _levels(state: persistence.State, dev: str, unit: int, values: list[str]) -> list[str]:
    keys = state.level_maps.setdefault(dev, {}).setdefault(str(unit), [])
    for value in values:
        if value not in keys:
            keys.append(value)  # append-only: a stored scene level must keep its meaning
    return keys


def _bare(value: object) -> str:
    return str(value).rsplit(".", 1)[-1]


def finite_number(value: object) -> float | None:
    """A finite, plausibly sized number, else None. json.loads accepts NaN, Infinity, 1e400
    (inf) and integers too large for a float, all of which arrive from Home Connect payloads."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and abs(number) <= _NUMBER_LIMIT else None


def _fmt(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}"


def _ceil_minutes(seconds: float) -> int:
    """Whole minutes, rounded up so a program never shows 0 while it still has seconds left."""
    return -(-int(seconds) // 60)


def _switch_on(value: model.Value | None) -> bool:
    # A boolean setting is on when True; PowerState is an enum whose On value is "...PowerState.On"
    # and whose Off and Standby both display as off (spec 6.2).
    return value is not None and (value.value is True or _bare(value.value) == "On")


def _door_open(value: model.Value | None) -> bool:
    # Closed and Locked are both closed for the contact; Locked shows in the state text instead.
    return value is not None and _bare(value.value) == "Open"


def constraints_of(detail: dict | None) -> dict:
    """The constraints dict of an option or setting description, {} when there is none."""
    constraints = detail.get("constraints") if detail is not None else None
    return constraints if isinstance(constraints, dict) else {}


def key_unit(value: model.Value | None, detail: dict) -> str:
    """A key's unit: from its reported value, else from the detail that declares it."""
    return (value.unit if value is not None else "") or str(detail.get("unit") or "")


def _shape(value: model.Value | None, detail: dict) -> str:
    """What a key holds: from its value, or from the program detail's declared type while no
    value has been reported (an option only another program offers, spec 6.3)."""
    raw = value.value if value is not None else None
    declared = detail.get("type")
    if isinstance(raw, bool) or (raw is None and declared == "Boolean"):
        return "bool"
    # A number shape even when the value is unusable (NaN, overflow): the kind is permanent.
    numeric = isinstance(raw, int | float) and not isinstance(raw, bool)
    if numeric or (raw is None and declared in ("Double", "Int")):
        return "number"
    return "other"


def _readonly_kind(key: str, value: model.Value | None, detail: dict) -> str:
    meter = catalog.meter(key)
    if meter:
        return meter.kind
    info = catalog.special(key)
    if info:
        return info.kind
    if catalog.is_compartment_door(key):
        return "door"
    if _shape(value, detail) == "number":
        units = {"°C": "temperature", "%": "percentage", "seconds": "minutes"}
        return units.get(key_unit(value, detail), "custom")
    return "text"


def _writable_kind(value: model.Value | None, detail: dict) -> str:
    shape = _shape(value, detail)
    if shape == "bool":
        return "switch"
    if shape == "number":
        return "setpoint"
    return "selector" if constraints_of(detail).get("allowedvalues") else "text"


def _offered_options(app: model.Appliance) -> list[str]:
    """Option keys of every cached program detail, in cache order."""
    keys: list[str] = []
    for detail in app.cache.program_details.values():
        options = detail.get("options") if isinstance(detail, dict) else None
        for option in options if isinstance(options, list) else []:
            key = option.get("key") if isinstance(option, dict) else None
            if text.valid_key(key) and key not in keys:
                keys.append(key)
    return keys


def value_label(app: model.Appliance, value: str, lang: str) -> str:
    """The level name of an enum value: a name the catalog or the appliance gives it, else the
    key's."""
    named = text.clean_label(
        catalog.value_name(app.value_names, value, lang) or "", _LEVEL_NAME_MAX
    )
    return named or catalog.humanize(value)


def program_label(app: model.Appliance, program: str, lang: str) -> str:
    """The level name of a program on the program selector."""
    return text.clean_label(catalog.name_for(app.names, program, lang), _LEVEL_NAME_MAX)


def _selector_options(names: list[str], **custom: str) -> dict[str, str]:
    """A selector's whole Options; `custom` adds keys or overrides a default."""
    return {
        "LevelNames": "|".join(names),
        "LevelActions": "|" * (len(names) - 1),
        "LevelOffHidden": "false",
        "SelectorStyle": "1",
        **custom,
    }


def _selector(
    state: persistence.State,
    app: model.Appliance,
    dev: str,
    unit: int,
    value: model.Value | None,
    detail: dict,
    lang: str,
    present: bool,
) -> tuple[int, dict]:
    # Keys are joined with "|" into HCKeys, so only well-formed keys may become levels.
    allowed = [v for v in constraints_of(detail).get("allowedvalues", []) if text.valid_key(v)]
    current = value.value if value is not None and text.valid_key(value.value) else None
    keys = _levels(state, dev, unit, allowed + ([current] if current else []))
    names = [catalog.word("not_available", lang)] + [value_label(app, k, lang) for k in keys]
    level = 10 * (keys.index(current) + 1) if present and current in keys else 0
    # The plugin owns the whole Options, custom keys included: Domoticz drops HCKeys when the user
    # saves the selector, and the device writer restores whatever differs from this (spec 4.4).
    return level, _selector_options(names, HCKeys="|" + "|".join(keys))


def setpoint_scale(unit_label: str) -> int:
    """Wire units per shown unit of a setpoint: 60 for seconds shown as minutes, else 1."""
    return SECONDS_PER_MINUTE if unit_label == "seconds" else 1


def setpoint_unit(unit_label: str, lang: str) -> str:
    return "min" if setpoint_scale(unit_label) > 1 else catalog.unit_text(unit_label, lang)


def scaled_bound(bound: float, scale: int, *, upper: bool) -> float:
    """A range bound in the card's unit. A converted bound rounds inwards to two decimals, so
    1 s reads as 0.02 min, a value that is itself inside the range."""
    if scale == 1:
        return bound
    shifted = bound / scale * 100
    return (math.floor(shifted) if upper else math.ceil(shifted)) / 100


def setpoint_svalue(wire: float, scale: int) -> str:
    """A setpoint's sValue: the wire value in the card's unit, to two decimals when converted
    (80 s is 1.33 min, not 1.3333333333333333)."""
    return str(float(wire if scale == 1 else round(wire / scale, 2)))


def _setpoint_options(detail: dict, unit_label: str, lang: str) -> dict[str, str]:
    options: dict[str, str] = {}
    constraints = constraints_of(detail)
    unit_label = text.clean_label(unit_label or str(detail.get("unit") or ""), _UNIT_TEXT_MAX)
    scale = setpoint_scale(unit_label)
    for src, dst in (("min", "ValueMin"), ("max", "ValueMax")):
        bound = finite_number(constraints.get(src))
        if bound is not None:
            options[dst] = _fmt(scaled_bound(bound, scale, upper=dst == "ValueMax"))
    step = finite_number(constraints.get("stepsize"))
    options["ValueStep"] = _fmt(step / scale) if step is not None and step > 0 else _DEFAULT_STEP
    if unit_label:
        options["ValueUnit"] = setpoint_unit(unit_label, lang)
    return options


def _number_options(kind: str, unit_label: str, lang: str) -> dict[str, str]:
    if kind == "minutes":
        return dict(_MINUTES_OPTIONS)
    unit_label = text.clean_label(catalog.unit_text(unit_label, lang), _UNIT_TEXT_MAX)
    return {"Custom": f"1;{unit_label}"} if kind == "custom" and unit_label else {}


def _meter_update(
    dev: str,
    unit: int,
    label: str,
    item: str,
    kind: str,
    meter: catalog.MeterInfo,
    number: float | None,
    lang: str,
) -> DeviceUpdate:
    if kind == "energy_total":
        options = {}
    else:
        options = {"ValueQuantity": catalog.pick(meter.quantity, lang), "ValueUnits": meter.units}
    if number is None:
        # Never created or written without a real total: Domoticz would log 0 and count the
        # whole lifetime as the first day's use.
        update = _update(dev, unit, label, item, kind, 0, "", options=options)
        return replace(update, create=False, has_value=False)
    whole = round(number / meter.divisor)
    svalue = f"0;{whole}" if kind == "energy_total" else str(whole)
    return _update(dev, unit, label, item, kind, 0, svalue, options=options)


def _value_update(
    state: persistence.State,
    app: model.Appliance,
    dev: str,
    unit: int,
    label: str,
    key: str,
    kind: str,
    value: model.Value | None,
    detail: dict,
    lang: str,
    present: bool,
) -> DeviceUpdate:
    item = catalog.name_for(app.names, key, lang)
    info = catalog.special(key)
    number = finite_number(value.value) if value is not None else None
    unit_label = key_unit(value, detail)
    meter = catalog.meter(key)
    if meter is not None and unit_label and unit_label != meter.reported:
        # A wrong unit would write a total 1000x off into a meter whose history cannot be corrected.
        number = None
    # A unit created with another kind (an older "custom" device) keeps it, and its branch below.
    if kind in _METER_KINDS and meter is not None:
        return _meter_update(dev, unit, label, item, kind, meter, number, lang)
    # Nothing usable reported: create the unit but write no value (see DeviceUpdate). A selector
    # is exempt, its level 0 "Not available" is a real state.
    unusable = value is None or (kind in _NUMERIC_KINDS and number is None)
    if unusable and kind != "selector":
        type_name, switchtype = _KIND_TYPES[kind]
        if kind == "setpoint":
            options = _setpoint_options(detail, unit_label, lang)
        else:
            options = _number_options(kind, unit_label, lang)
        return DeviceUpdate(
            dev,
            unit,
            _name(label, item),
            type_name,
            0,
            "",
            options=options,
            switchtype=switchtype,
            deadband=info.deadband if info else 0.0,
            has_value=False,
        )
    if kind == "switch":
        return _binary(dev, unit, label, item, kind, _switch_on(value))
    if kind == "door":
        return _binary(dev, unit, label, item, kind, _door_open(value))
    if kind == "selector":
        level, options = _selector(state, app, dev, unit, value, detail, lang, present)
        return _update(dev, unit, label, item, kind, level, str(level), options=options)
    if kind == "setpoint":
        options = _setpoint_options(detail, unit_label, lang)
        scale = setpoint_scale(unit_label)
        svalue = setpoint_svalue(number, scale) if number is not None else ""
        return _update(dev, unit, label, item, kind, 0, svalue, options=options)
    if kind in ("temperature", "percentage", "custom", "minutes"):
        if number is not None and kind == "minutes":
            number = float(_ceil_minutes(number))
        if number is not None and info and info.resolution:
            number = round(number / info.resolution) * info.resolution
        svalue = _fmt(number) if number is not None else ""
        options = _number_options(kind, unit_label, lang)
        deadband = info.deadband if info else 0.0
        return _update(dev, unit, label, item, kind, 0, svalue, options=options, deadband=deadband)
    raw = value.value if value is not None else ""
    shown = catalog.value_name(app.value_names, raw, lang) if isinstance(raw, str) else None
    if isinstance(raw, bool):
        shown = catalog.word("yes" if raw else "no", lang)
    elif not shown and isinstance(raw, str) and ".EnumType." in raw and text.valid_key(raw):
        shown = catalog.humanize(raw)  # an enum value Home Connect sent no name for
    shown = text.clean_label(shown or _bare(raw), _TEXT_MAX)
    return _update(dev, unit, label, item, "text", 0, text.escape(shown))


def _main_setting(key: str) -> bool:
    """Settings that are an appliance's main controls and so stay shown: a fridge-freezer has no
    programs, and its setpoints and super modes are what a user operates it by."""
    last = key.rsplit(".", 1)[-1]
    return key.startswith("Refrigeration.") and last.startswith(
        ("SetpointTemperature", "SuperMode")
    )


def _dynamic(update: DeviceUpdate, key: str, kind: str) -> DeviceUpdate:
    """Every dynamic unit carries its Home Connect key and kind in its own Options, so a lost
    snapshot is rebuilt from the devices themselves, never guessed from names (spec 4.4). Whether
    it starts used is catalog.used_at_creation."""
    shown = catalog.used_at_creation(key, _main_setting(key))
    return replace(
        update, options={**update.options, "HCKey": key, "HCKind": kind}, used=1 if shown else 0
    )


def _option_number(app: model.Appliance, key: str) -> float | None:
    value = app.options.get(key)
    return finite_number(value.value) if value is not None else None


def _finish_at(app: model.Appliance, state: str | None, now: float) -> float | None:
    """Wall time the program finishes, or None when that is not known.

    Each countdown counts from when it was received, not from now: anchored to now, the finish
    time would creep later between events and snap back on the next one, rewriting the text
    (and a history row) for nothing. A value set without a receive time falls back to now.
    """

    def anchored(key: str) -> float | None:
        seconds = _option_number(app, key)
        return app.received.get(key, now) + seconds if seconds is not None else None

    remaining = _option_number(app, catalog.REMAINING)
    if state == "DelayedStart":
        # Inferred from the docs, to verify live (spec 6.2 unit 6): laundry counts FinishInRelative
        # to the end of the program; ovens and dishwashers count StartInRelative to its start, so
        # the program's own remaining time comes on top.
        finish = anchored(catalog.FINISH_IN)
        if finish is not None:
            return finish
        start = anchored(catalog.START_IN)
        return start + remaining if start is not None and remaining is not None else None
    return anchored(catalog.REMAINING) if app.active_program else None


def _phase(app: model.Appliance, state: str | None) -> str:
    """Which run the cards for units 4 to 6 describe: "running", "finished" or "idle".

    No reported state yet counts as running, so nothing is reset before Home Connect has said
    anything. An active program the model still holds does not count once the state is idle, and
    a cleared active program ends the run even while the state event lags.
    """
    if state in model.IDLE_STATES:
        return "idle"
    if state == "Finished":
        return "finished"
    if state is None or app.active_program or state == "DelayedStart":
        return "running"
    return "idle"


def _run_cards(
    app: model.Appliance, dev: str, label: str, lang: str, now: float, state: str | None
) -> list[DeviceUpdate]:
    """Units 4 to 6: progress, remaining time and program (spec 6.2).

    Outside a run they are reset instead of keeping the last run's values. The reset values are
    derived from a reported OperationState, which is what spec 6.4 allows to be written. A reset
    whose key was not reported in this run (always the case right after a restart, since these
    values are not persisted) is planned with create=False: it clears a unit that already exists
    and never creates one, so an appliance without programs never gains these units. The model
    keeps the last countdown; it is only ignored here.

    A disconnected appliance has reported no state that ends the run, so its cards are left as
    they were (spec 7.4: DISCONNECTED writes nothing but unit 1), with one exception: a "ready
    at" time that has passed is never shown, so the program text drops it. A model that knows no
    state and no run (just after a restart) resets the program text, connected or not, so a
    stored "ready at" cannot outlive the run it described.
    """
    out: list[DeviceUpdate] = []
    phase = _phase(app, state)
    progress_item = catalog.word("progress", lang)
    time_item = catalog.word("remaining", lang)
    program_item = catalog.word("program", lang)
    has_time = catalog.REMAINING in app.options or catalog.ELAPSED in app.options

    def time_update(minutes: int) -> DeviceUpdate:
        options = dict(_MINUTES_OPTIONS)
        return _update(
            dev, UNIT_TIME, label, time_item, "minutes", 0, str(minutes), options=options
        )

    def program_text(svalue: str) -> DeviceUpdate:
        return _update(dev, UNIT_PROGRAM, label, program_item, "text", 0, svalue)

    def name_of(program: str | None) -> str:
        return text.escape(catalog.name_for(app.names, program, lang)) if program else ""

    def reset(update: DeviceUpdate, reported: bool) -> DeviceUpdate:
        return update if reported else replace(update, create=False)

    def finish_passed(program: str | None) -> bool:
        finish_at = _finish_at(app, state, now)
        return bool(program) and finish_at is not None and finish_at <= now

    has_progress = catalog.PROGRESS in app.options
    selected = name_of(app.selected_program)
    selected_suffix = f" ({catalog.word('selected', lang)})"
    idle_program = program_text(selected + selected_suffix if selected else "-")
    if state is None and app.active_program is None and not (has_progress or has_time):
        if not app.has_programs():
            return out
        return [reset(idle_program, app.selected_program is not None)]
    program = app.active_program or app.selected_program
    if not app.connected:
        if phase == "running" and finish_passed(program):
            suffix = "" if app.active_program else selected_suffix
            out.append(reset(program_text(name_of(program) + suffix), False))
        return out
    if phase == "idle":
        idle_progress = _update(dev, UNIT_PROGRESS, label, progress_item, "percentage", 0, "0")
        out.append(reset(idle_progress, has_progress))
        out.append(reset(time_update(0), has_time))
        out.append(reset(idle_program, app.has_programs()))
        return out
    if phase == "finished":
        if has_progress:  # a progress never reported is left as it is
            out.append(_update(dev, UNIT_PROGRESS, label, progress_item, "percentage", 0, "100"))
        out.append(reset(time_update(0), has_time))
        shown = name_of(program)
        note = catalog.word("finished", lang)
        finished = program_text(f"{shown}, {note}" if shown else note.capitalize())
        out.append(reset(finished, app.has_programs()))
        return out
    progress = app.options.get(catalog.PROGRESS)
    percent = finite_number(progress.value) if progress is not None else None
    if percent is not None:
        # An appliance can report a value outside a percentage; the gauge only shows 0..100.
        percent = min(max(percent, 0.0), 100.0)
        out.append(
            _update(dev, UNIT_PROGRESS, label, progress_item, "percentage", 0, _fmt(percent))
        )
    remaining = app.options.get(catalog.REMAINING) or app.options.get(catalog.ELAPSED)
    seconds = finite_number(remaining.value) if remaining is not None else None
    if seconds is not None:
        out.append(time_update(_ceil_minutes(seconds)))
    finish_at = _finish_at(app, state, now)
    if program and finish_at is not None and finish_at > now:
        estimated = app.options.get(catalog.ESTIMATED)
        about = f"{catalog.word('about', lang)} " if estimated and estimated.value is True else ""
        # Rounded to the minute, so the text changes only when the finish time moves by one.
        finish = round(finish_at / 60) * 60
        out.append(
            program_text(
                f"{name_of(program)}, {catalog.word('ready_at', lang)} {about}{clock(finish)}"
            )
        )
    elif program:
        # A finish time already passed (the countdown stopped arriving) is never shown.
        suffix = "" if app.active_program else selected_suffix
        out.append(program_text(name_of(program) + suffix))
    return out


def _status_code(
    app: model.Appliance, dev: str, label: str, lang: str, state: str | None
) -> DeviceUpdate | None:
    """Unit 11, or None for an unknown or missing state, which leaves the level as it is.

    Created used, because dzVents and Lua load only used devices, and named with a leading "$",
    which keeps a used device off the dashboard while scripts still read it and trigger on it.
    Both apply at creation only, so a user's own choice survives. Its levels live in code, never
    in HCKeys, so the level map recovery never reports it (domoticz_api.recover_from_devices).
    """
    level = STATUS_CODE_OFFLINE if not app.connected else STATUS_CODE_LEVELS.get(state or "")
    if level is None:
        return None
    names = [catalog.word("unknown", lang)]
    names += [catalog.state_name(s, lang) for s in STATUS_CODE_STATES]
    names.append(catalog.word("offline", lang))
    options = _selector_options(names, LevelOffHidden="true")
    item = catalog.word("status_code", lang)
    update = _update(
        dev, UNIT_STATUS_CODE, label, item, "selector", level, str(level), options=options
    )
    return replace(update, name=f"${update.name}")


def _fixed(
    app: model.Appliance, dev: str, label: str, lang: str, now: float, stale: bool
) -> list[DeviceUpdate]:
    out: list[DeviceUpdate] = []
    state = app.operation_state()
    program = app.active_program or app.selected_program
    program_name = text.escape(catalog.name_for(app.names, program, lang)) if program else ""
    state_item = catalog.word("state", lang)
    door = app.status.get(catalog.DOOR_STATE)
    if not app.connected:
        offline = catalog.word("offline", lang)
        out.append(_update(dev, UNIT_STATE, label, state_item, "alert", catalog.GREY, offline))
    elif state:
        # An unknown state is shown humanized, so its text comes from Home Connect.
        parts = [text.escape(text.clean_label(catalog.state_name(state, lang), _LABEL_MAX))]
        if app.active_program:
            parts.append(program_name)
        if door and _bare(door.value) == "Locked":
            parts.append(catalog.word("locked", lang))
        svalue = ", ".join(parts) + (f" ({catalog.word('last_known', lang)})" if stale else "")
        colour = catalog.STATE_COLOUR.get(state, catalog.GREY)
        out.append(_update(dev, UNIT_STATE, label, state_item, "alert", colour, svalue))
    power = app.settings.get(catalog.POWER_STATE)
    if power is not None:
        item = catalog.word("power", lang)
        out.append(_binary(dev, UNIT_POWER, label, item, "switch", _switch_on(power)))
    if door is not None:
        item = catalog.word("door", lang)
        out.append(_binary(dev, UNIT_DOOR, label, item, "door", _door_open(door)))
    out += _run_cards(app, dev, label, lang, now, state)
    code = _status_code(app, dev, label, lang, state)
    if code is not None:
        out.append(code)
    remote_keys = (catalog.LOCAL_CONTROL, catalog.REMOTE_START, catalog.REMOTE_ACTIVE)
    remote = [app.status.get(k) for k in remote_keys]
    if any(v is not None for v in remote):
        local, start, active = (v is not None and v.value is True for v in remote)
        if local:
            level, word_key = catalog.ORANGE, "local"
        elif start:
            level, word_key = catalog.GREEN, "start_allowed"
        else:
            level, word_key = catalog.GREY, "remote_on" if active else "remote_off"
        item = catalog.word("remote", lang)
        out.append(
            _update(dev, UNIT_REMOTE, label, item, "alert", level, catalog.word(word_key, lang))
        )
    return out


def _no_device(key: str, value: model.Value | None) -> bool:
    """Keys Home Connect uses internally (program history and details: identifiers, packed
    records) and structured values, which no Domoticz device can show; a unit created for one
    earlier is left alone and no longer updated (no unit is ever deleted, spec 6.4)."""
    if any(part in key for part in _INTERNAL_KEY_PARTS):
        return True
    return value is not None and isinstance(value.value, dict | list)


def _program_selector(
    app: model.Appliance, state: persistence.State, dev: str, label: str, lang: str
) -> DeviceUpdate:
    """Unit 8: the selected program, or the one that runs, as a stable level (spec 6.3)."""
    running = app.active_program and _phase(app, app.operation_state()) != "idle"
    current = app.active_program if running else app.selected_program
    candidates = [*app.cache.programs, app.selected_program, app.active_program]
    keys = _levels(state, dev, UNIT_PROGRAM_SELECT, [k for k in candidates if text.valid_key(k)])
    names = ["-"] + [program_label(app, k, lang) for k in keys]
    level = 10 * (keys.index(current) + 1) if current in keys else 0
    # The plugin owns the whole Options, HCKeys included (spec 4.4).
    options = _selector_options(names, HCKeys="|" + "|".join(keys))
    item = catalog.word("program_select", lang)
    return _update(
        dev, UNIT_PROGRAM_SELECT, label, item, "selector", level, str(level), options=options
    )


def _door_actions(app: model.Appliance) -> bool:
    # Only an oven with a motorised door lists them; other ovens answer UnsupportedCommand.
    return bool(_DOOR_COMMANDS & set(app.cache.commands))


def _actions(app: model.Appliance, dev: str, label: str, lang: str) -> DeviceUpdate:
    doors = _door_actions(app)
    shown = [name for name in ACTION_LEVELS if doors or name not in _DOOR_ACTIONS]
    names = ["-"] + [catalog.word(f"action_{name}", lang) for name in shown]
    item = catalog.word("actions", lang)
    options = _selector_options(names)
    return _update(dev, UNIT_ACTIONS, label, item, "selector", 0, "0", options=options)


def _acknowledge(dev: str, label: str, lang: str) -> DeviceUpdate:
    # A push button has no state of its own: created, never given a value.
    item = catalog.word("acknowledge", lang)
    return replace(_update(dev, UNIT_ACK, label, item, "push", 0, "Off"), has_value=False)


def _delayed_start(
    app: model.Appliance, state: persistence.State, dev: str, label: str, lang: str
) -> DeviceUpdate | None:
    key = delay_key(app)
    if key is None:
        return None
    names = ["-"] + [clock_text(m) for m in delay_times()]
    # The held time is the plugin's own choice, not a Home Connect value (spec 6.3).
    level = delay_level(state.delayed_start.get(app.ha_id))
    item = catalog.word("ready_at_name" if key == catalog.FINISH_IN else "start_at_name", lang)
    options = _selector_options(names)
    return _update(
        dev, UNIT_DELAYED_START, label, item, "selector", level, str(level), options=options
    )


def _control(
    app: model.Appliance,
    state: persistence.State,
    dev: str,
    label: str,
    lang: str,
    allow_control: bool,
    blocked: Container[tuple[str, int]],
) -> list[DeviceUpdate]:
    out: list[DeviceUpdate] = []
    if app.has_programs():
        # A program selector whose level keys were lost is left exactly as it is: a fresh map
        # from today's program list would give a stored scene or timer level another program,
        # and writing HCKeys would unblock it at the next start (spec 4.4).
        if (dev, UNIT_PROGRAM_SELECT) not in blocked:
            out.append(_program_selector(app, state, dev, label, lang))
        if allow_control:
            out.append(_actions(app, dev, label, lang))
            delayed = _delayed_start(app, state, dev, label, lang)
            if delayed is not None:
                out.append(delayed)
    if allow_control and catalog.ACKNOWLEDGE in app.cache.commands:
        out.append(_acknowledge(dev, label, lang))
    return out


def plan_appliance(
    app: model.Appliance,
    state: persistence.State,
    lang: str,
    now_wall: float,
    stream_stale: bool,
    label: str,
    warnings: list[str] | None = None,
    *,
    allow_control: bool = False,
    blocked: Container[tuple[str, int]] = (),
) -> list[DeviceUpdate]:
    """Device updates for one appliance. A key that finds no free unit is skipped and reported
    in `warnings` (one entry per key per call, no haId) for the caller to log once, since this
    module cannot log (spec 6.2). `blocked` holds the (DeviceID, unit) selectors whose level
    keys could not be recovered (domoticz_api.recover_from_devices)."""
    dev = device_id(app.ha_id)
    out = [
        replace(u, used=int(allow_control)) if u.unit in CONTROL_SHOWN_UNITS else u
        for u in _fixed(app, dev, label, lang, now_wall, stream_stale)
    ]
    out += _control(app, state, dev, label, lang, allow_control, blocked)

    def allocate(key: str) -> int | None:
        unit = _allocate(state, dev, key)
        if unit is None and warnings is not None:
            warnings.append(
                f"unit_exhausted appliance={text.log_value(label)} key={text.log_value(key)}"
            )
        return unit

    def emit(
        key: str,
        wanted: str,
        value: model.Value | None,
        detail: dict,
        present: bool,
        create: bool = True,
    ) -> None:
        if _no_device(key, value):
            return
        unit = allocate(key)
        # A blocked selector is never planned, for the reason _control gives for unit 8.
        if unit is None or (dev, unit) in blocked:
            return
        kind = _kind(state, dev, unit, wanted)
        update = _value_update(
            state, app, dev, unit, label, key, kind, value, detail, lang, present
        )
        planned = _dynamic(update, key, kind)
        out.append(planned if create else replace(planned, create=False))

    idle = _phase(app, app.operation_state()) == "idle"
    for key, value in app.status.items():
        if key in catalog.FIXED_KEYS:
            continue
        if idle and key in catalog.RUN_SCOPED:
            # Derived from a reported OperationState, like the run cards (spec 6.4).
            value = model.Value(catalog.RUN_SCOPED[key])
        emit(key, _readonly_kind(key, value, {}), value, {}, True)
    if idle:
        # Not reported since a restart (these values are not persisted), but the device may still
        # show the last run's value: reset a unit that exists, never create one.
        for key, reset in catalog.RUN_SCOPED.items():
            if key not in app.status and key in state.unit_alloc.get(dev, {}):
                value = model.Value(reset)
                emit(key, _readonly_kind(key, value, {}), value, {}, True, create=False)
    for key, value in app.settings.items():
        if key in catalog.FIXED_KEYS:
            continue
        capability = app.capability(key)
        if capability == "setting_unknown":
            continue  # its writability decides the device type, which can never change later
        detail = {"constraints": app.cache.setting_constraints.get(key, {})}
        if capability == "setting_rw":
            wanted = _writable_kind(value, detail)
        else:
            wanted = _readonly_kind(key, value, detail)
        emit(key, wanted, value, detail, True)
    details_complete = all(p in app.cache.program_details for p in app.cache.programs)
    if details_complete and app.cache.programs:
        # One device per option key, created the first time any program offers it or the
        # selected or active program reports it; one that is not offered now shows "Not
        # available" instead of disappearing (spec 6.3).
        offered = _offered_options(app)
        candidates = dict.fromkeys(app.options)
        candidates.update(dict.fromkeys(offered))
        allocated = state.unit_alloc.get(dev, {})
        kinds = state.unit_kinds.get(dev, {})
        candidates.update(dict.fromkeys(sorted(k for k in allocated if ".Option." in k)))
        for key in candidates:
            capability = app.capability(key)
            if key in catalog.FIXED_KEYS or capability == "start_only":
                continue
            if (
                key not in app.options
                and key not in offered
                and kinds.get(str(allocated.get(key))) == "text"
            ):
                # A text unit with nothing reported and nothing offering it has no value to
                # show, and it may have been created for a structured value, which must never
                # be updated again (_no_device); after a restart only its kind is left to tell.
                continue
            value = app.options.get(key)
            detail = model.option_detail(app, key) or {}
            if capability == "selectable":
                wanted = _writable_kind(value, detail)
            else:
                wanted = _readonly_kind(key, value, detail)
            emit(key, wanted, value, detail, key in app.options)
    for key, ev in app.events.items():
        unit = allocate(key)
        if unit is None:
            continue
        present = ev.value == model.PRESENT
        if catalog.is_favourite(key):
            kind = _kind(state, dev, unit, "push")
            name = app.names.get(key) or key.split(".Event.", 1)[-1]
            out.append(_dynamic(_binary(dev, unit, label, name, kind, present), key, kind))
            continue
        kind = _kind(state, dev, unit, "alert")
        item = catalog.name_for(app.names, key, lang)
        shown = text.escape(item)
        level = (
            catalog.EVENT_LEVEL_COLOUR.get(ev.level, catalog.YELLOW) if present else catalog.GREY
        )
        svalue = shown if present else catalog.word("no_notification", lang)
        update = _update(dev, unit, label, item, kind, level, svalue)
        out.append(_dynamic(update, key, kind))
    return _unique_names(_with_descriptions(_with_images(out, state, dev, app.type), app, lang))


@functools.cache
def _status_code_levels(lang: str) -> str:
    levels = [f"{STATUS_CODE_LEVELS[s]} {catalog.state_name(s, lang)}" for s in STATUS_CODE_STATES]
    levels.append(f"{STATUS_CODE_OFFLINE} {catalog.word('offline', lang)}")
    return ", ".join(levels)


def _dynamic_description(key: str, kind: str) -> tuple[str, bool]:
    """(description name, sends commands) for a unit allocated per key. The kind, permanent once
    created, tells a writable device from a read-only one."""
    if kind in _METER_KINDS:
        return "total", False
    writable = kind in _WRITABLE_KINDS
    if catalog.is_favourite(key):
        return "favourite", False
    if ".Event." in key:
        return "event", False
    if ".Setting." in key:
        return ("setting", True) if writable else ("setting_reading", False)
    if ".Option." in key:
        return ("option", True) if writable else ("option_reading", False)
    return "reading", False


@functools.cache
def _description(name: str, lang: str, control: bool) -> str:
    """Planned on every pass but used only when a unit is created, so each text is built once
    per language and then reused."""
    return catalog.description(name, lang, control, levels=_status_code_levels(lang))


def _with_descriptions(
    updates: list[DeviceUpdate], app: model.Appliance, lang: str
) -> list[DeviceUpdate]:
    out = []
    for update in updates:
        key = update.options.get("HCKey")
        if key:
            name, control = _dynamic_description(key, update.options.get("HCKind", ""))
        elif update.unit == UNIT_DELAYED_START:
            name = "ready_at" if delay_key(app) == catalog.FINISH_IN else "start_at"
            control = True
        elif update.unit == UNIT_ACTIONS:
            name, control = ("actions_doors" if _door_actions(app) else "actions"), True
        else:
            name, control = _FIXED_DESCRIPTIONS[update.unit], update.unit in _COMMAND_UNITS
        out.append(replace(update, description=_description(name, lang, control)))
    return out


def _with_images(
    updates: list[DeviceUpdate], state: persistence.State, dev: str, appliance_type: str
) -> list[DeviceUpdate]:
    kinds = state.unit_kinds.get(dev, {})
    out = []
    for update in updates:
        kind = _FIXED_KINDS.get(update.unit) or kinds.get(str(update.unit), "")
        image = catalog.image_for(kind, appliance_type)
        out.append(replace(update, image=image) if image else update)
    return out


def _unique_names(updates: list[DeviceUpdate]) -> list[DeviceUpdate]:
    """Give every unit of one device its own name (spec 6.4).

    Keys can still share a name (two the catalog names alike, or two Home Connect names alike),
    so whatever collides gets " (2)", " (3)" and so on. Fixed units keep their names, then the
    lowest unit number wins, so a name never moves to another unit between passes. The writer
    renames an existing unit only while it still carries the name the plugin gave it, so a
    user's name survives.
    """
    seen: set[str] = set()
    final: dict[int, DeviceUpdate] = {}
    for index in sorted(
        range(len(updates)), key=lambda i: ("HCKey" in updates[i].options, updates[i].unit)
    ):
        update = updates[index]
        name, number = update.name, 2
        while name.casefold() in seen:
            suffix = f" ({number})"
            name = text.clean_label(update.name, _NAME_MAX - len(suffix)) + suffix
            number += 1
        seen.add(name.casefold())
        final[index] = update if name == update.name else replace(update, name=name)
    return [final[i] for i in range(len(updates))]


def apply_overlay(
    updates: list[DeviceUpdate], overlay: Mapping[tuple[str, int], tuple[int, str]]
) -> list[DeviceUpdate]:
    """Show commanded values at once, as requests, until Home Connect confirms or refuses them
    (spec 7.6): the one sanctioned exception to writing only reported values (spec 6.4)."""
    out = []
    for update in updates:
        shown = overlay.get((update.device_id, update.unit))
        if shown is None:
            out.append(update)
        else:
            out.append(replace(update, nvalue=shown[0], svalue=shown[1], has_value=True))
    return out


# Short on purpose: the card is a small tile, and a longer text needs a scrollbar.
_ACCOUNT_TEXT = {
    "AUTHORIZED": ("Signed in", "Aangemeld"),
    "UNAUTHORIZED": ("Starting sign-in", "Aanmelden wordt gestart"),
    "AWAITING_USER": ("Sign-in needed", "Aanmelden nodig"),
    "EXPIRED_CODE": (
        'Code expired: choose "Sign in again"',
        'Code verlopen: kies "Opnieuw aanmelden"',
    ),
    "NEEDS_REAUTH": (
        'Sign-in lost: choose "Sign in again"',
        'Aanmelding verloren: kies "Opnieuw aanmelden"',
    ),
    "NEEDS_USER_ACTION": ("Action needed: {message}", "Actie nodig: {message}"),
    "SIGNED_OUT": (
        "Signed out (turn off Sign out to sign in)",
        "Afgemeld (zet Sign out uit om je aan te melden)",
    ),
}
# While a Sign in again is pending the "choose Sign in again" hint has been followed already.
_ACCOUNT_PENDING = {
    "EXPIRED_CODE": ("Code expired", "Code verlopen"),
    "NEEDS_REAUTH": ("Sign-in lost", "Aanmelding verloren"),
}
_ACTION_NEEDED = ("Action needed", "Actie nodig")
_CHOOSE_REAUTHORIZE = ('Choose "Sign in again"', 'Kies "Opnieuw aanmelden"')
_AGAIN = ("Again: {link}", "Opnieuw: {link}")
_CODE = ("Code {code} (until {until})", "Code {code} (tot {until})")
_CODE_ONLY = ("Code {code}", "Code {code}")

# Domoticz runs Text and Alert sValues through DOMPurify and renders the result as HTML
# (sanitizeHTML in www/js/domoticzdevices.js, released since 2026.1), so an anchor and a list
# reach the card. Every interpolated value is still escaped: the sValue is also read back by
# dzVents and the JSON API, which sanitize nothing. No styling and no whitespace between tags:
# colouring and spacing are the theme's job.


def link_markup(url: str, lang: str) -> str:
    """The verification link as an anchor with a short label, or "" when there is none.

    Callers only pass a URL that text.auth_link accepted (https on a Home Connect host); the
    full URL would make the card long and hard to read, so it is the href only.
    """
    if not url:
        return ""
    label = text.escape(catalog.word("login_link", lang))
    return f'<a href="{text.escape_attribute(url)}" target="_blank">{label}</a>'


def bullets(items: Iterable[str]) -> str:
    """Markup fragments as a list; empty items are skipped and an empty list emits nothing.

    Items are markup, so the caller escapes every value it interpolates into them.
    """
    listed = "".join(f"<li>{item}</li>" for item in items if item)
    return f"<ul>{listed}</ul>" if listed else ""


def _account_text(view: AccountView, lang: str) -> str:
    def pick(pair: tuple[str, str]) -> str:
        return pair[1] if lang == "nl" else pair[0]

    link = link_markup(view.link, lang)
    pending = bool(link or view.user_code)
    message = text.escape(view.message)
    if view.state == "NEEDS_USER_ACTION" and not message:
        line = pick(_ACTION_NEEDED)
    elif pending and view.state in _ACCOUNT_PENDING:
        line = pick(_ACCOUNT_PENDING[view.state])
    else:
        line = pick(_ACCOUNT_TEXT.get(view.state, (view.state, view.state))).format(message=message)
    # NEEDS_USER_ACTION carries its message in the line; any other note gets its own bullet.
    note = message if view.state != "NEEDS_USER_ACTION" else ""
    if link and view.state != "AWAITING_USER":
        # A Re-authorize pending in a state that keeps its tokens (spec 8.5).
        link = pick(_AGAIN).format(link=link)
    code = ""
    if view.user_code:
        template = pick(_CODE if view.valid_until else _CODE_ONLY)
        code = template.format(
            code=text.escape(view.user_code), until=text.escape(view.valid_until)
        )
    hint = ""
    if view.state == "NEEDS_USER_ACTION" and not pending:
        hint = pick(_CHOOSE_REAUTHORIZE)
    # The code stays visible: it is what the user types when the link is opened on another
    # device.
    return line + bullets([link, code, hint, note])


def plan_account(view: AccountView, lang: str, shown: bool = True) -> list[DeviceUpdate]:
    """The account units. `shown` is their Used value when they are created, so a unit recreated
    while signed in starts hidden like the rest; an existing unit's Used is never touched."""
    message = _account_text(view, lang)
    names = ["-", catalog.word("reauthorize", lang), catalog.word("refresh", lang)]
    actions = _selector_options(names, SelectorStyle="0")
    return [
        DeviceUpdate(
            ACCOUNT_DEVICE,
            ACCOUNT_TEXT,
            catalog.word("account", lang),
            "Text",
            0,
            message,
            used=int(shown),
            description=catalog.description("account", lang),
        ),
        DeviceUpdate(
            ACCOUNT_DEVICE,
            ACCOUNT_ACTIONS,
            catalog.word("account_actions", lang),
            "Selector Switch",
            0,
            "0",
            options=actions,
            switchtype=18,
            used=int(shown),
            description=catalog.description("account_actions", lang),
        ),
        DeviceUpdate(
            ACCOUNT_DEVICE,
            ACCOUNT_CALLS,
            catalog.word("calls", lang),
            "Custom",
            0,
            str(view.calls),
            options={"Custom": f"1;{catalog.unit_text('calls', lang)}"},
            used=0,
            description=catalog.description("calls", lang),
        ),
    ]
