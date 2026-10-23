"""The user's five appliances, replayed from scrubbed captures through the real modules.

Discovery, the daily refresh and the captured event stream run through model and scheduler the
way the plugin dispatches them (every answer reaches the model before the scheduler sees it), with
each request answered from the capture; the result goes through planner and the Domoticz adapter
into the fake Domoticz. Offline and fast: nothing here touches the network or the real clock.
"""

import json
from collections import Counter, deque
from pathlib import Path

import budget
import catalog
import domoticz_api
import errors
import model
import persistence
import planner
import scheduler
import sse

DIR = Path(__file__).parent / "fixtures"
# Just after the captured stream's event timestamps, so countdowns read as they did live.
WALL = 1_792_049_000.0
LANG = "en"
# Generous bound for discovery of five appliances; a scheduling loop would blow straight past it.
MAX_REQUESTS = 200
CAPTURE_KEY = {
    "status": "status",
    "settings": "settings",
    "available": "programs/available",
    "selected": "programs/selected",
    "active": "programs/active",
    "commands": "commands",
}
DRYER_PROGRAM = "LaundryCare.Dryer.Program.Cotton"


def _captures() -> tuple[dict, dict[str, dict]]:
    listing = json.loads((DIR / "appliances" / "_list.json").read_text(encoding="utf-8"))
    caps = {}
    for path in sorted((DIR / "appliances").glob("*.json")):
        if path.name != "_list.json":
            cap = json.loads(path.read_text(encoding="utf-8"))
            caps[cap["appliance"]["haId"]] = cap
    return listing, caps


def _setting_detail(cap: dict, key: str) -> tuple[int, object]:
    """The captures hold no setting details: answer a boolean setting as writable and refuse
    the rest, so both the detail and the definite-refusal path run."""
    settings = cap["settings"]["body"]["data"]["settings"]
    value = next((s.get("value") for s in settings if s.get("key") == key), None)
    if isinstance(value, bool):
        return 200, {"data": {"key": key, "constraints": {"access": "readWrite"}}}
    return 403, {"error": {"key": "SDK.Error.UnsupportedSetting", "description": "refused"}}


def _stream_lines() -> list[str]:
    return json.loads((DIR / "stream.json").read_text(encoding="utf-8"))


class Replay:
    def __init__(self, state: persistence.State | None = None):
        self.listing, self.caps = _captures()
        self.model = model.Model(wall=lambda: WALL)
        self.sched = scheduler.Scheduler(lambda: 1000.0, lambda: WALL)
        self.state = state if state is not None else persistence.State()
        self.requests: Counter[str] = Counter()
        self.sent: list[scheduler.Job] = []
        self.pending: deque[scheduler.Job] = deque()

    def submit(self, jobs: list[scheduler.Job]) -> None:
        for job in jobs:
            # The worker queue coalesces jobs that share a dedupe key.
            if job.dedupe_key and any(p.dedupe_key == job.dedupe_key for p in self.pending):
                continue
            self.pending.append(job)

    def answer(self, job: scheduler.Job) -> tuple[int, object]:
        if job.kind == "list":
            return self.listing["status"], self.listing["body"]
        cap = self.caps[job.ha_id]
        if job.kind == "setting_detail":
            return _setting_detail(cap, job.key)
        entry = cap["program_details"][job.key] if job.kind == "program_detail" else None
        entry = entry or cap[CAPTURE_KEY[job.kind]]
        return entry["status"], entry["body"]

    def apply(self, job: scheduler.Job, status: int, body: object) -> None:
        m, ha = self.model, job.ha_id
        error = errors.parse_api_error(status, json.dumps(body).encode("utf-8"))
        if status != 200 and error.category == errors.OFFLINE:
            m.set_offline(ha)
        data = body if status == 200 else None
        if job.kind == "list":
            # As plugin._apply_list: the list adds appliances and links their persisted caches.
            if status == 200:
                new = m.apply_list(data, job.issued_seq)
                for app in m.appliances.values():
                    app.cache = self.state.caches.setdefault(app.ha_id, app.cache)
                self.submit(self.sched.discovery(m, new))
            return
        m.apply_answer(
            job.kind,
            ha,
            status,
            data,
            key=job.key,
            issued=job.issued_seq,
            state_stamp=job.state_stamp,
        )

    def drain(self) -> None:
        while self.pending:
            job = self.pending.popleft()
            self.requests[job.kind] += 1
            self.sent.append(job)
            assert self.requests.total() < MAX_REQUESTS, self.requests
            status, body = self.answer(job)
            self.apply(job, status, body)
            self.submit(self.sched.after_response(self.model, job, status))

    def discover(self) -> None:
        self.submit([self.sched.list_job(self.model)])
        self.drain()
        # daily() hands out one appliance per call; it runs dry once every cache is filled.
        for _ in range(len(self.model.appliances) + 1):
            jobs = self.sched.daily(self.model, WALL)
            if not jobs:
                break
            self.submit(jobs)
            self.drain()
        assert self.sched.daily(self.model, WALL) == []

    def stream(self) -> list[scheduler.Job]:
        parser = sse.Parser()
        jobs: list[scheduler.Job] = []
        for line in _stream_lines():
            event = parser.feed((line + "\n").encode("utf-8"))
            if event is not None:
                jobs += self.sched.on_effect(self.model, self.model.apply_event(event))
        self.submit(jobs)
        self.drain()
        return jobs

    def plan(self) -> tuple[list[planner.DeviceUpdate], list[str], dict[str, str]]:
        warnings: list[str] = []
        labels = planner.display_names(self.model.appliances.values(), warnings)
        updates: list[planner.DeviceUpdate] = []
        for app in self.model.appliances.values():
            label = labels[app.ha_id]
            updates += planner.plan_appliance(app, self.state, LANG, WALL, False, label, warnings)
        return updates, warnings, labels

    def by_type(self, kind: str) -> list[model.Appliance]:
        return sorted(
            (a for a in self.model.appliances.values() if a.type == kind), key=lambda a: a.ha_id
        )


def _full_replay(domoticz) -> tuple[Replay, dict[str, str]]:
    replay = Replay()
    replay.discover()
    replay.stream()
    updates, warnings, labels = replay.plan()
    assert warnings == []
    domoticz_api.apply_updates(domoticz.Devices, updates, replay.state)
    return replay, labels


def _units(domoticz, app: model.Appliance) -> dict:
    return domoticz.Devices[planner.device_id(app.ha_id)].Units


def test_discovery_requests_only_what_the_captures_answer():
    replay = Replay()
    replay.discover()
    m = replay.model
    assert len(m.appliances) == 5
    ovens = replay.by_type("Oven")
    assert sorted(len(a.cache.programs) for a in ovens) == [12, 16]
    for oven in ovens:
        assert set(oven.cache.programs) == set(oven.cache.program_details)
        assert oven.cache.commands
    # Only the idle ovens may list their programs: the dryer is running, the washer has finished
    # and the dishwasher answered that it is offline.
    assert replay.requests["available"] == 2
    assert replay.requests["program_detail"] == 28
    (dishwasher,) = replay.by_type("Dishwasher")
    assert dishwasher.connected is False
    (dryer,) = replay.by_type("Dryer")
    assert dryer.operation_state() == "Run"
    assert dryer.active_program == DRYER_PROGRAM
    assert dryer.cache.programs == []


def test_mid_run_program_list_never_shrinks_the_known_programs():
    replay = Replay()
    replay.discover()
    (dryer,) = replay.by_type("Dryer")
    known = ["LaundryCare.Dryer.Program.Mix", DRYER_PROGRAM, "LaundryCare.Dryer.Program.Synthetic"]
    dryer.cache.programs = list(known)
    captured = replay.caps[dryer.ha_id]["programs/available"]["body"]
    assert len(captured["data"]["programs"]) == 1
    replay.model.apply_available(dryer.ha_id, captured, "Run")
    assert dryer.cache.programs == known
    replay.model.apply_available(dryer.ha_id, captured, "Ready")
    assert dryer.cache.programs == known


def test_all_five_appliances_get_sensible_devices(domoticz):
    replay, labels = _full_replay(domoticz)
    for app in replay.model.appliances.values():
        units = _units(domoticz, app)
        assert planner.UNIT_STATE in units
        assert all(1 <= n <= planner.MAX_UNIT for n in units)
        assert len(units) < 80
        names = [unit.Name.casefold() for unit in units.values()]
        assert len(set(names)) == len(names), "unit names are unique per device"
        for unit in units.values():
            assert unit.Name.removeprefix("$").startswith(labels[app.ha_id])
            assert app.ha_id not in unit.Name + str(unit.sValue)
            assert "<" not in str(unit.sValue)
    for oven in replay.by_type("Oven"):
        units = _units(domoticz, oven)
        assert {planner.UNIT_STATE, planner.UNIT_DOOR, planner.UNIT_POWER} <= set(units)
        options = [k for k in replay.state.unit_alloc[oven.ha_id] if ".Option." in k]
        assert options, "an idle oven with complete program details gets option devices"
    (dishwasher,) = replay.by_type("Dishwasher")
    offline = catalog.word("offline", LANG)
    assert _units(domoticz, dishwasher)[planner.UNIT_STATE].sValue == offline
    (dryer,) = replay.by_type("Dryer")
    dryer_units = _units(domoticz, dryer)
    assert {planner.UNIT_TIME, planner.UNIT_PROGRAM} <= set(dryer_units)
    assert int(dryer_units[planner.UNIT_TIME].sValue) > 0
    (washer,) = replay.by_type("Washer")
    assert washer.operation_state() == "Finished"
    assert planner.UNIT_PROGRAM in _units(domoticz, washer)


def test_second_pass_writes_nothing(domoticz):
    replay, _ = _full_replay(domoticz)
    before = {
        (d, n): len(u.updates) for d, dev in domoticz.Devices.items() for n, u in dev.Units.items()
    }
    updates, _, _ = replay.plan()
    assert domoticz_api.apply_updates(domoticz.Devices, updates, replay.state) == 0
    after = {
        (d, n): len(u.updates) for d, dev in domoticz.Devices.items() for n, u in dev.Units.items()
    }
    assert after == before


def test_restart_reuses_persisted_caches_and_devices(domoticz):
    first, _ = _full_replay(domoticz)
    count = sum(len(dev.Units) for dev in domoticz.Devices.values())
    saved, fell_back = persistence.loads(persistence.dumps(first.state))
    assert not fell_back
    assert domoticz_api.recover_from_devices(domoticz.Devices, saved) == []
    second = Replay(saved)
    second.discover()
    # The program lists and details came back from the state, so nothing refetches them.
    assert second.requests["available"] == 0
    assert second.requests["program_detail"] == 0
    assert second.requests["setting_detail"] == 0
    second.stream()
    updates, warnings, _ = second.plan()
    assert warnings == []
    assert domoticz_api.apply_updates(domoticz.Devices, updates, second.state) == 0
    assert sum(len(dev.Units) for dev in domoticz.Devices.values()) == count


def test_captured_stream_replays_without_refetch_storm():
    replay = Replay()
    replay.discover()
    jobs = replay.stream()
    assert len(jobs) <= 2 * len(replay.model.appliances)
    (oven_with_event,) = [
        a for a in replay.by_type("Oven") if "Cooking.Oven.Event.EcolysisRequired" in a.events
    ]
    assert oven_with_event.events["Cooking.Oven.Event.EcolysisRequired"].value == model.PRESENT
    (dryer,) = replay.by_type("Dryer")
    assert "BSH.Common.Option.RemainingProgramTime" in dryer.options


def test_monitor_mode_fetches_and_records_every_setting_constraint(domoticz):
    # Spec 7.3 step 6: a setting's device type is fixed at creation, so its constraints are
    # fetched whatever AllowControl says, once per key, from the background class.
    replay = Replay()
    replay.discover()
    replay.stream()
    details = [j for j in replay.sent if j.kind == "setting_detail"]
    assert details
    per_key = Counter((j.ha_id, j.key) for j in details)
    assert set(per_key.values()) == {1}, "one detail fetch per setting key"
    assert {j.budget_class for j in details} == {budget.BACKGROUND}
    capabilities = Counter()
    for app in replay.model.appliances.values():
        for key in app.settings:
            assert key in app.cache.setting_constraints
            capabilities[app.capability(key)] += 1
    assert capabilities["setting_rw"] > 0 and capabilities["setting_ro"] > 0
    assert set(capabilities) == {"setting_rw", "setting_ro"}
    updates, warnings, _ = replay.plan()
    assert warnings == []
    domoticz_api.apply_updates(domoticz.Devices, updates, replay.state)
    for app in replay.model.appliances.values():
        names = [unit.Name.casefold() for unit in _units(domoticz, app).values()]
        assert len(set(names)) == len(names), "unit names are unique per device"
        allocated = replay.state.unit_alloc.get(app.ha_id, {})
        for key in app.settings:
            if key not in catalog.FIXED_KEYS:
                assert key in allocated, "a setting with known writability gets a device"


def test_washer_settings_get_devices_with_control_off(domoticz):
    replay, _ = _full_replay(domoticz)
    (washer,) = replay.by_type("Washer")
    allocated = replay.state.unit_alloc[washer.ha_id]
    wanted = [
        "BSH.Common.Setting.ChildLock",
        "LaundryCare.Washer.Setting.IDos1BaseLevel",
        "LaundryCare.Washer.Setting.IDos2BaseLevel",
    ]
    units = _units(domoticz, washer)
    for key in wanted:
        assert key in allocated, key
        assert allocated[key] in units, key
