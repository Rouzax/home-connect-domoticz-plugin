"""Appliance state from REST payloads and stream events (spec 4.1). Pure.

The model holds only OBSERVED values (spec 7.6). Every value carries the sequence number of the
observation that set it; a REST answer applies a key only when no newer observation of that key
arrived after the request was issued, so a slow GET can never roll back a stream event (spec 4.3).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import errors
import persistence
import sse
import text

PRESENT = "BSH.Common.EnumType.EventPresentState.Present"
START_ONLY = frozenset({"BSH.Common.Option.StartInRelative", "BSH.Common.Option.FinishInRelative"})
IDLE_STATES = frozenset({"Inactive", "Ready"})
OPERATION_STATE = "BSH.Common.Status.OperationState"
SELECTED_ROOT = "BSH.Common.Root.SelectedProgram"
ACTIVE_ROOT = "BSH.Common.Root.ActiveProgram"
# Key in Appliance.seq for the last connection event. Not a valid Home Connect key, so it can
# never collide with an observed one.
CONNECTION_STAMP = "@connection"


@dataclass(frozen=True)
class Value:
    value: object
    unit: str = ""


@dataclass(frozen=True)
class EventState:
    value: str
    level: str
    handling: str


@dataclass(frozen=True)
class EventEffect:
    ha_id: str | None
    kind: str
    program_changed: str | None = None
    unknown_settings: tuple[str, ...] = ()
    unknown_appliance: bool = False


def _option_list(detail: object) -> list[dict]:
    options = detail.get("options") if isinstance(detail, dict) else None
    return [o for o in options if isinstance(o, dict)] if isinstance(options, list) else []


@dataclass
class Appliance:
    ha_id: str
    name: str = ""
    brand: str = ""
    type: str = ""
    vib: str = ""
    connected: bool = False
    # Offline because an API answer said so (409 Offline), not because of a stream event. No
    # CONNECTED event need follow when it comes back, and the fetch that answer refused is
    # still missing.
    offline_by_rest: bool = False
    # Back online from such an answer: the scheduler queues one capped refetch and clears this.
    resync_due: bool = False
    status: dict[str, Value] = field(default_factory=dict)
    settings: dict[str, Value] = field(default_factory=dict)
    options: dict[str, Value] = field(default_factory=dict)
    selected_program: str | None = None
    active_program: str | None = None
    events: dict[str, EventState] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    value_names: dict[str, str] = field(default_factory=dict)
    seq: dict[str, int] = field(default_factory=dict)
    # Wall time each observed value was received. A countdown (remaining time, start or finish
    # in) is relative to that moment, not to whenever it is read (spec 6.2 unit 6).
    received: dict[str, float] = field(default_factory=dict)
    cache: persistence.ApplianceCache = field(default_factory=persistence.ApplianceCache)

    def operation_state(self) -> str | None:
        value = self.status.get(OPERATION_STATE)
        return str(value.value).rsplit(".", 1)[-1] if value and value.value else None

    def is_idle(self) -> bool:
        state = self.operation_state()
        return state is None or state in IDLE_STATES

    def has_programs(self) -> bool:
        return bool(self.cache.programs or self.selected_program or self.active_program)

    def capability(self, key: str) -> str:
        if ".Setting." in key:
            constraints = self.cache.setting_constraints.get(key)
            if constraints is None:
                return "setting_unknown"
            return "setting_rw" if constraints.get("access") == "readWrite" else "setting_ro"
        if key in START_ONLY:
            return "start_only"
        option = option_detail(self, key)
        if option is None:
            return "readonly"
        constraints = option.get("constraints")
        access = (
            constraints.get("access", "readWrite") if isinstance(constraints, dict) else "readWrite"
        )
        return "selectable" if access == "readWrite" else "readonly"


def option_detail(app: Appliance, key: str) -> dict | None:
    """The cached description of option `key` (type, unit, constraints), or None.

    Constraints change with the program selection (spec 6.3), so the selected program's detail
    wins, then the active program's; any other cached program is only a fallback for an option
    that neither of them lists.
    """
    details = app.cache.program_details
    ordered = [p for p in (app.selected_program, app.active_program) if p is not None]
    ordered += [p for p in details if p not in ordered]
    for program in ordered:
        for option in _option_list(details.get(program)):
            if option.get("key") == key:
                return option
    return None


def _data(payload) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def _entries(payload, name: str) -> list[dict]:
    found = _data(payload).get(name)
    return [e for e in found if isinstance(e, dict)] if isinstance(found, list) else []


class Model:
    def __init__(self, wall: Callable[[], float] = time.time):
        self.appliances: dict[str, Appliance] = {}
        self._seq = 0
        self._wall = wall

    def tick(self) -> int:
        self._seq += 1
        return self._seq

    def _observe(self, app: Appliance, store: dict, entry: dict, issued: int | None) -> str | None:
        key = entry.get("key")
        if not text.valid_key(key):
            return None
        if issued is not None and app.seq.get(key, 0) > issued:
            return None  # a newer observation already arrived
        store[key] = Value(entry.get("value"), str(entry.get("unit", "") or ""))
        app.seq[key] = self.tick()
        app.received[key] = self._wall()
        self._remember_names(app, entry)
        return key

    def _remember_names(self, app: Appliance, entry: dict) -> None:
        if isinstance(entry.get("name"), str) and entry["name"]:
            app.names[entry["key"]] = text.clean_label(entry["name"])
        value, shown = entry.get("value"), entry.get("displayvalue")
        if text.valid_key(value) and isinstance(shown, str) and shown:
            app.value_names[value] = text.clean_label(shown)

    def apply_list(self, payload, issued: int) -> list[str]:
        new = []
        for entry in _entries(payload, "homeappliances"):
            ha_id = entry.get("haId")
            if not text.valid_ha_id(ha_id):
                continue
            if ha_id not in self.appliances:
                self.appliances[ha_id] = Appliance(ha_id)
                new.append(ha_id)
            app = self.appliances[ha_id]
            app.name = text.clean_label(entry.get("name") or "")
            app.brand = text.clean_label(entry.get("brand") or "")
            app.type = text.clean_label(entry.get("type") or "")
            app.vib = text.clean_label(entry.get("vib") or "")
            if app.seq.get(CONNECTION_STAMP, 0) <= issued:
                if entry.get("connected") is True:
                    self._back_online(app)
                else:
                    app.connected = app.offline_by_rest = app.resync_due = False
        return new

    def _back_online(self, app: Appliance, resync: bool = False) -> None:
        if resync or app.offline_by_rest:
            app.resync_due = True
        app.connected, app.offline_by_rest = True, False

    def _seen_alive(self, app: Appliance, issued: int | None, resync: bool = False) -> None:
        """Mark an appliance connected that answered 200 or sent a live item: Home Connect sends
        no CONNECTED after an offline answer to a GET, and a stream outage can swallow the one
        it sends after a DISCONNECTED. `issued` is the sequence number of the request that was
        answered (None for a stream item, which is always current): an answer to a request sent
        before the appliance was marked offline says nothing about now. `resync` asks for the
        refetch an offline answer would ask for; nothing was fetched while it was away."""
        if app.connected:
            return
        if issued is not None and app.seq.get(CONNECTION_STAMP, 0) > issued:
            return
        self._back_online(app, resync)
        app.seq[CONNECTION_STAMP] = self.tick()

    def apply_status(self, ha_id: str, payload, issued: int) -> None:
        app = self.appliances.get(ha_id)
        if app is not None:
            for entry in _entries(payload, "status"):
                self._observe(app, app.status, entry, issued)

    def apply_settings(self, ha_id: str, payload, issued: int) -> None:
        app = self.appliances.get(ha_id)
        if app is not None:
            for entry in _entries(payload, "settings"):
                self._observe(app, app.settings, entry, issued)

    def apply_program(self, ha_id: str, kind: str, payload, issued: int) -> None:
        app = self.appliances.get(ha_id)
        if app is None:
            return
        root = ACTIVE_ROOT if kind == "active" else SELECTED_ROOT
        if app.seq.get(root, 0) > issued:
            return  # a program event arrived after this request was issued
        data = _data(payload) if payload is not None else {}
        key = data.get("key") if text.valid_key(data.get("key")) else None
        if kind == "active":
            app.active_program = key
        else:
            app.selected_program = key
        app.seq[root] = self.tick()
        if key and isinstance(data.get("name"), str):
            app.names[key] = text.clean_label(data["name"])
        # Options describe the active program while one runs, otherwise the selected one.
        if key and (kind == "active" or app.active_program is None):
            for entry in _option_list(data):
                self._observe(app, app.options, entry, issued)

    def apply_available(self, ha_id: str, payload, state_stamp: str | None) -> None:
        app = self.appliances.get(ha_id)
        # A running or finished appliance lists only some programs (spec 3.2): only an answer to a
        # request issued while idle may extend the list, and nothing ever removes from it.
        if app is None or (state_stamp is not None and state_stamp not in IDLE_STATES):
            return
        for entry in _entries(payload, "programs"):
            key = entry.get("key")
            if text.valid_key(key):
                if key not in app.cache.programs:
                    app.cache.programs.append(key)
                if isinstance(entry.get("name"), str):
                    app.names[key] = text.clean_label(entry["name"])

    def apply_program_detail(self, ha_id: str, key: str, payload) -> None:
        app = self.appliances.get(ha_id)
        data = _data(payload)
        if app is not None and text.valid_key(key) and data:
            app.cache.program_details[key] = data
            for option in _option_list(data):
                if text.valid_key(option.get("key")):
                    self._remember_names(app, option)

    def apply_setting_detail(self, ha_id: str, key: str, payload) -> None:
        app = self.appliances.get(ha_id)
        data = _data(payload)
        if app is not None and text.valid_key(key) and data:
            constraints = data.get("constraints")
            app.cache.setting_constraints[key] = (
                constraints if isinstance(constraints, dict) else {}
            )

    def apply_commands(self, ha_id: str, payload) -> None:
        app = self.appliances.get(ha_id)
        if app is None:
            return
        for entry in _entries(payload, "commands"):
            key = entry.get("key")
            if text.valid_key(key) and key not in app.cache.commands:
                app.cache.commands.append(key)

    def apply_answer(
        self,
        kind: str,
        ha_id: str,
        status: int,
        data: object,
        *,
        key: str = "",
        issued: int = 0,
        state_stamp: str | None = None,
    ) -> None:
        """Apply one REST answer for an appliance job of `kind` (spec 7.3, 7.4).

        `data` is the decoded body of a 200 answer and None otherwise. Only an answer that says
        something about the appliance is applied: a 404 on a program endpoint means "nothing
        selected or active", and a definite refusal of a setting's detail (any 4xx but a timeout,
        a 409 offline or state answer, or a rate limit) records it as read-only so its device can
        still be created, its type being decided once (spec 6.4). Anything else changes nothing
        and is left for a later settings answer or daily refresh. The appliance list is not
        handled here: it adds appliances, which is the caller's business.
        """
        app = self.appliances.get(ha_id)
        if app is not None and status == 200:
            self._seen_alive(app, issued)
        if kind == "status" and status == 200:
            self.apply_status(ha_id, data, issued)
        elif kind == "settings" and status == 200:
            self.apply_settings(ha_id, data, issued)
        elif kind in ("selected", "active") and status in (200, 404):
            self.apply_program(ha_id, kind, data, issued)
        elif kind == "available" and status == 200:
            self.apply_available(ha_id, data, state_stamp)
        elif kind == "program_detail" and status == 200:
            self.apply_program_detail(ha_id, key, data)
        elif kind == "setting_detail" and status == 200:
            self.apply_setting_detail(ha_id, key, data)
        elif kind == "setting_detail" and 400 <= status < 500 and status not in (408, 409, 429):
            self.apply_setting_detail(ha_id, key, {"data": {"constraints": {}}})
        elif kind == "commands" and status == 200:
            self.apply_commands(ha_id, data)

    def set_offline(self, ha_id: str) -> None:
        app = self.appliances.get(ha_id)
        if app is not None:
            app.connected, app.offline_by_rest, app.resync_due = False, True, False
            app.seq[CONNECTION_STAMP] = self.tick()

    def apply_event(self, event: sse.Event) -> EventEffect:
        kind = {
            "STATUS": "status",
            "NOTIFY": "notify",
            "EVENT": "event",
            "CONNECTED": "connected",
            "DISCONNECTED": "disconnected",
            "PAIRED": "paired",
            "DEPAIRED": "depaired",
            "KEEP-ALIVE": "keepalive",
        }.get(event.type, "other")
        if kind == "keepalive":
            return EventEffect(None, kind)
        ha_id = event.id
        payload = errors.parse_json(event.data.encode("utf-8")) if event.data else None
        if isinstance(payload, dict):
            payload_id = payload.get("haId")
            if text.valid_ha_id(payload_id):
                ha_id = payload_id
        if not text.valid_ha_id(ha_id):
            return EventEffect(None, kind)
        app = self.appliances.get(ha_id)
        if app is None:
            return EventEffect(
                ha_id, kind, unknown_appliance=kind not in ("depaired", "disconnected")
            )
        if kind in ("connected", "paired", "disconnected", "depaired"):
            # A connection event decides on its own; CONNECTED and PAIRED refetch the appliance
            # (scheduler.on_effect), so no resync is left pending.
            app.connected = kind in ("connected", "paired")
            app.offline_by_rest = app.resync_due = False
            app.seq[CONNECTION_STAMP] = self.tick()
            return EventEffect(ha_id, kind)
        changed: str | None = None
        unknown: list[str] = []
        for item in sse.items(event):
            key = item.get("key")
            if not text.valid_key(key):
                continue
            if app.offline_by_rest:
                self._seen_alive(app, None)
            elif kind == "status" or (kind == "notify" and item.get("value") is not None):
                # Only live items: EVENT items are replayed on every stream connect (spec 3.3),
                # and switching off sends SelectedProgram = None while the appliance leaves.
                self._seen_alive(app, None, resync=True)
            if kind == "event":
                app.events[key] = EventState(
                    str(item.get("value", "")),
                    str(item.get("level", "")),
                    str(item.get("handling", "")),
                )
                app.seq[key] = self.tick()
            elif key in (SELECTED_ROOT, ACTIVE_ROOT):
                value = item.get("value")
                program = value if text.valid_key(value) else None
                if key == SELECTED_ROOT:
                    app.selected_program = program
                else:
                    app.active_program = program
                app.seq[key] = self.tick()
                if program:
                    changed = "selected" if key == SELECTED_ROOT else "active"
            elif ".Setting." in key:
                if key not in app.settings:
                    unknown.append(key)
                self._observe(app, app.settings, item, None)
            elif ".Option." in key:
                self._observe(app, app.options, item, None)
            else:
                self._observe(app, app.status, item, None)
        return EventEffect(ha_id, kind, changed, tuple(unknown))
