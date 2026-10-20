"""Durable plugin state to and from JSON for Domoticz.Configuration(). Pure."""

import json
import math
from dataclasses import asdict, dataclass, field

import text

STATE_VERSION = 1
STATE_KEY = "state"
FIRST_DYNAMIC_UNIT = 20


@dataclass
class Tokens:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    scope: str
    client_id_hash: str


@dataclass
class ApplianceCache:
    programs: list[str] = field(default_factory=list)
    program_details: dict[str, dict] = field(default_factory=dict)
    setting_constraints: dict[str, dict] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)
    negative_keys: list[str] = field(default_factory=list)
    refreshed_at: float = 0.0
    # Home Connect's own display names (key or value key to label). Without them a restart
    # names units from the catalog fallback until an answer carrying names arrives, renaming
    # each unit twice.
    names: dict[str, str] = field(default_factory=dict)
    value_names: dict[str, str] = field(default_factory=dict)


@dataclass
class State:
    tokens: Tokens | None = None
    auth_state: str = "UNAUTHORIZED"
    last_sign_out: bool = False
    next_unit: dict[str, int] = field(default_factory=dict)
    unit_alloc: dict[str, dict[str, int]] = field(default_factory=dict)
    level_maps: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    unit_kinds: dict[str, dict[str, str]] = field(default_factory=dict)
    auto_names: dict[str, str] = field(default_factory=dict)
    caches: dict[str, ApplianceCache] = field(default_factory=dict)
    ledger: dict[str, dict[str, int]] = field(default_factory=dict)
    block_until: float = 0.0
    block_length: float = 0.0
    account_shown: bool = True
    # Whether the control-only units were last shown; they are hidden or shown once when
    # AllowControl changes (spec 6.4).
    control_shown: bool = False
    # haId to the held delayed-start clock time, in minutes after midnight (spec 6.3).
    delayed_start: dict[str, int] = field(default_factory=dict)


def dumps(state: State) -> str:
    payload = asdict(state)
    payload["version"] = STATE_VERSION
    return json.dumps(payload, separators=(",", ":"))


def _is_num(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_minute_of_day(value) -> bool:
    return _is_int(value) and 0 <= value < 24 * 60


def _float_or_zero(value) -> float:
    return float(value) if _is_num(value) else 0.0


def _str_list(node) -> list[str] | None:
    if not isinstance(node, list) or not all(isinstance(x, str) for x in node):
        return None
    return list(node)


def _typed_map(node, check) -> dict:
    """Keep only entries with a string key whose value passes `check`; a wrong shape yields {}."""
    if not isinstance(node, dict):
        return {}
    return {k: v for k, v in node.items() if isinstance(k, str) and check(v)}


def _tokens(node) -> Tokens | None:
    if not isinstance(node, dict):
        return None
    try:
        tokens = Tokens(**node)
    except TypeError:
        return None
    text_fields = (tokens.access_token, tokens.refresh_token, tokens.scope, tokens.client_id_hash)
    if not all(isinstance(x, str) for x in text_fields) or not _is_num(tokens.expires_at):
        return None
    return tokens


def _labels(node) -> dict[str, str]:
    """Display names as the model holds them: valid keys, cleaned and bounded labels. A missing
    or malformed map is empty rather than costing the cache: older snapshots have none."""
    if not isinstance(node, dict):
        return {}
    cleaned = {k: text.clean_label(v) for k, v in node.items() if isinstance(v, str)}
    return {k: v for k, v in cleaned.items() if text.valid_key(k) and v}


def _cache(node) -> ApplianceCache | None:
    if not isinstance(node, dict):
        return None
    programs = _str_list(node.get("programs"))
    commands = _str_list(node.get("commands"))
    negative = _str_list(node.get("negative_keys"))
    details = node.get("program_details")
    constraints = node.get("setting_constraints")
    refreshed = node.get("refreshed_at")
    if programs is None or commands is None or negative is None:
        return None
    if not _is_num(refreshed):
        return None
    if not isinstance(details, dict) or not isinstance(constraints, dict):
        return None
    return ApplianceCache(
        programs,
        details,
        constraints,
        commands,
        negative,
        _float_or_zero(refreshed),
        _labels(node.get("names")),
        _labels(node.get("value_names")),
    )


def _raise_next_unit(state: State) -> None:
    """Never let the allocator hand out a unit that unit_alloc already uses.

    next_unit is dropped when malformed while unit_alloc may survive; it is only ever raised.
    """
    for device, alloc in state.unit_alloc.items():
        if alloc:
            floor = max(alloc.values()) + 1
            state.next_unit[device] = max(state.next_unit.get(device, FIRST_DYNAMIC_UNIT), floor)


def loads(text: str) -> tuple[State, bool]:
    """Never raises: this comes from a database column and is read during onStart.

    Returns (state, fell_back). fell_back is True only when the payload as a whole is unusable;
    a single malformed part (one cache, the tokens) is dropped on its own, so a bad rebuildable
    entry never costs the critical unit and level maps.
    """
    if not text or not text.strip():
        return State(), True
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError):
        return State(), True
    if not isinstance(raw, dict):
        return State(), True
    # Version dispatch goes here when STATE_VERSION first changes; v1 has nothing to migrate.
    auth_state = raw.get("auth_state")
    state = State(
        tokens=_tokens(raw.get("tokens")),
        auth_state=auth_state if isinstance(auth_state, str) else "UNAUTHORIZED",
        last_sign_out=raw.get("last_sign_out") is True,
        next_unit=_typed_map(raw.get("next_unit"), _is_int),
        unit_alloc={
            dev: _typed_map(units, _is_int)
            for dev, units in _typed_map(
                raw.get("unit_alloc"), lambda v: isinstance(v, dict)
            ).items()
        },
        level_maps={
            dev: _typed_map(units, lambda v: _str_list(v) is not None)
            for dev, units in _typed_map(
                raw.get("level_maps"), lambda v: isinstance(v, dict)
            ).items()
        },
        unit_kinds={
            dev: _typed_map(units, lambda v: isinstance(v, str))
            for dev, units in _typed_map(
                raw.get("unit_kinds"), lambda v: isinstance(v, dict)
            ).items()
        },
        auto_names=_typed_map(raw.get("auto_names"), lambda v: isinstance(v, str)),
        caches={
            ha: cache
            for ha, node in _typed_map(raw.get("caches"), lambda v: isinstance(v, dict)).items()
            if (cache := _cache(node)) is not None
        },
        ledger={
            cls: _typed_map(minutes, _is_int)
            for cls, minutes in _typed_map(raw.get("ledger"), lambda v: isinstance(v, dict)).items()
        },
        block_until=_float_or_zero(raw.get("block_until")),
        block_length=_float_or_zero(raw.get("block_length")),
        account_shown=raw.get("account_shown") is not False,
        control_shown=raw.get("control_shown") is True,
        delayed_start=_typed_map(raw.get("delayed_start"), _is_minute_of_day),
    )
    _raise_next_unit(state)
    return state, False
