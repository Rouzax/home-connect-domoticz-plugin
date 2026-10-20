import json

import budget
import model
import scheduler
import sse
import workers

OVEN, FRIDGE, WASHER = "SIEMENS-OVEN-1", "SIEMENS-FRIDGE-1", "BOSCH-WASHER-1"
OP = "BSH.Common.Status.OperationState"
CHILD_LOCK = "BSH.Common.Setting.ChildLock"


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.wall = 500_000.0


def setup(*apps):
    m = model.Model()
    entries = [
        {"haId": h, "name": h, "type": t, "brand": "X", "vib": "V", "connected": True}
        for h, t in apps
    ]
    m.apply_list({"data": {"homeappliances": entries}}, m.tick())
    clock = Clock()
    return m, scheduler.Scheduler(mono=lambda: clock.t, wall=lambda: clock.wall), clock


def set_state(m, ha, state):
    payload = {
        "data": {"status": [{"key": OP, "value": f"BSH.Common.EnumType.OperationState.{state}"}]}
    }
    m.apply_status(ha, payload, m.tick())


def kinds(jobs):
    return [(j.kind, j.ha_id) for j in jobs]


def notify(ha, items):
    return sse.Event("NOTIFY", json.dumps({"haId": ha, "items": items}), ha)


def test_discovery_starts_with_status_and_settings_for_connected_appliances():
    m, s, _ = setup((OVEN, "Oven"), (FRIDGE, "FridgeFreezer"))
    m.appliances[FRIDGE].connected = False
    jobs = s.discovery(m, [OVEN, FRIDGE])
    assert kinds(jobs) == [("status", OVEN), ("settings", OVEN)]
    assert all(j.budget_class == budget.BACKGROUND for j in jobs)
    assert jobs[0].path == "/api/homeappliances/SIEMENS-OVEN-1/status"


def test_after_status_of_idle_program_appliance():
    m, s, _ = setup((OVEN, "Oven"))
    set_state(m, OVEN, "Ready")
    status_job = s.discovery(m, [OVEN])[0]
    jobs = s.after_response(m, status_job, 200)
    assert ("available", OVEN) in kinds(jobs)
    assert ("commands", OVEN) in kinds(jobs)
    assert ("active", OVEN) not in kinds(jobs)  # Ready: no active program to fetch
    available = next(j for j in jobs if j.kind == "available")
    assert available.state_stamp == "Ready"


def test_restart_with_persisted_caches_fetches_selected_once_from_discovery():
    m, s, _ = setup((OVEN, "Oven"))
    cache = m.appliances[OVEN].cache
    cache.programs.append("P.A")
    cache.commands.append("BSH.Common.Command.PauseProgram")
    jobs = s.discovery(m, [OVEN])
    assert kinds(jobs) == [("status", OVEN), ("selected", OVEN), ("settings", OVEN)]
    set_state(m, OVEN, "Ready")
    assert s.after_response(m, jobs[0], 200) == []


EXPECTED_ERROR_PRONE = {"selected", "active"}


def restart_six_idle_appliances(interleave: bool) -> tuple[list[str], int]:
    """Discovery for six idle ovens with persisted caches, answered as an idle appliance answers
    (selected: 404 NoProgramSelected). Returns the sent kinds and the highest brake count.

    interleave=False drains the whole queue before any answer is applied, as a worker does
    between two heartbeats; True applies each answer before the next job is sent."""
    apps = [(f"SIEMENS-HB0000000-00000000000{i}", "Oven") for i in range(6)]
    m, s, _ = setup(*apps)
    for app in m.appliances.values():
        app.cache.programs.append("Cooking.Oven.Program.HeatingMode.HotAir")
        app.cache.commands.append("BSH.Common.Command.AcknowledgeEvent")
        app.cache.setting_constraints[CHILD_LOCK] = {"access": "readWrite"}
    queue = workers.JobQueue(lambda: 0.0)
    brake = budget.Budget(wall=lambda: 500_000.0, mono=lambda: 1000.0)
    sent: list[str] = []
    worst = 0
    ready = {"data": {"status": [{"key": OP, "value": "BSH.Common.EnumType.OperationState.Ready"}]}}
    settings = {"data": {"settings": [{"key": CHILD_LOCK, "value": False}]}}

    def answer(job):
        nonlocal worst
        code = 404 if job.kind in EXPECTED_ERROR_PRONE else 200
        body = {"status": ready, "settings": settings}.get(job.kind)
        m.apply_answer(job.kind, job.ha_id, code, body, issued=job.issued_seq)
        brake.record(error=code >= 400)
        worst = max(worst, brake._consecutive)
        for follow in s.after_response(m, job, code):
            queue.put(follow)

    for job in s.discovery(m, list(m.appliances)):
        queue.put(job)
    while len(queue):
        batch = []
        while (job := queue.get(0)) is not None:
            sent.append(job.kind)
            if interleave:
                answer(job)
            else:
                batch.append(job)
        for job in batch:
            answer(job)
    return sent, worst


def test_restart_never_sends_expected_errors_back_to_back():
    for interleave in (False, True):
        sent, worst = restart_six_idle_appliances(interleave)
        assert sent.count("selected") == 6, sent
        pairs = zip(sent, sent[1:], strict=False)
        adjacent = [p for p in pairs if set(p) <= EXPECTED_ERROR_PRONE]
        assert adjacent == [], sent
        assert worst < budget.BRAKE_PAUSE


def test_after_status_of_running_appliance_fetches_active_not_available():
    m, s, _ = setup((WASHER, "Washer"))
    set_state(m, WASHER, "Run")
    jobs = s.after_response(m, s.discovery(m, [WASHER])[0], 200)
    assert ("active", WASHER) in kinds(jobs)
    assert ("available", WASHER) not in kinds(jobs)


def test_appliance_answering_409_at_discovery_waits_for_connected():
    m, s, _ = setup((OVEN, "Oven"))
    status_job = s.discovery(m, [OVEN])[0]
    assert s.after_response(m, status_job, 409) == []  # offline: no dependent fetches
    eff = m.apply_event(sse.Event("CONNECTED", "", OVEN))
    assert ("status", OVEN) in kinds(s.on_effect(m, eff))


def test_powered_off_at_first_discovery_is_scheduled_later_without_breaking_discovery():
    m, s, clock = setup((OVEN, "Oven"), (WASHER, "Washer"))
    oven_status, _oven_settings, washer_status, _ = s.discovery(m, [OVEN, WASHER])
    set_state(m, WASHER, "Ready")
    # The oven is switched off: status 409, programs 404/409. None of it may raise or queue work.
    assert s.after_response(m, oven_status, 409) == []
    assert s.after_response(m, scheduler.Job(kind="selected", ha_id=OVEN), 404) == []
    assert s.after_response(m, scheduler.Job(kind="available", ha_id=OVEN), 409) == []
    assert s.after_response(m, scheduler.Job(kind="commands", ha_id=OVEN), 409) == []
    # Failed fills are retried by daily() an hour out, not a day out.
    due = clock.wall + scheduler.EMPTY_PROGRAMS_RETRY_S
    assert m.appliances[OVEN].cache.refreshed_at + scheduler.DAILY_REFRESH_S == due
    # The other appliance still gets its dependent jobs.
    assert ("available", WASHER) in kinds(s.after_response(m, washer_status, 200))
    # When the oven comes online its jobs are scheduled from the event.
    eff = m.apply_event(sse.Event("CONNECTED", "", OVEN))
    jobs = s.on_effect(m, eff)
    assert kinds(jobs) == [("status", OVEN), ("settings", OVEN)]
    set_state(m, OVEN, "Ready")
    assert ("available", OVEN) in kinds(s.after_response(m, jobs[0], 200))


def test_appliance_without_programs_never_gets_program_jobs_but_gets_commands():
    m, s, _ = setup((FRIDGE, "FridgeFreezer"))
    set_state(m, FRIDGE, "Ready")
    jobs = s.after_response(m, s.discovery(m, [FRIDGE])[0], 200)
    assert not {"available", "selected", "active"} & {k for k, _ in kinds(jobs)}
    assert ("commands", FRIDGE) in kinds(jobs)


def test_cooling_and_hob_types_are_excluded():
    assert {"FridgeFreezer", "Refrigerator", "Freezer", "WineCooler", "Hob"} <= (
        scheduler.NO_PROGRAM_TYPES
    )
    assert "Cooktop" not in scheduler.NO_PROGRAM_TYPES
    assert not {"Oven", "Washer", "Dryer", "Dishwasher", "CoffeeMaker"} & scheduler.NO_PROGRAM_TYPES


def test_available_answer_queues_selected_and_missing_details():
    m, s, _ = setup((OVEN, "Oven"))
    set_state(m, OVEN, "Ready")
    m.apply_available(OVEN, {"data": {"programs": [{"key": "P.A"}, {"key": "P.B"}]}}, "Ready")
    m.appliances[OVEN].cache.program_details["P.A"] = {"options": []}
    job = scheduler.Job(kind="available", ha_id=OVEN, state_stamp="Ready")
    jobs = s.after_response(m, job, 200)
    assert kinds(jobs)[0] == ("selected", OVEN)
    details = [(j.kind, j.key) for j in jobs if j.kind == "program_detail"]
    assert details == [("program_detail", "P.B")]


def test_discovery_fills_stamp_the_daily_refresh_clock():
    m, s, clock = setup((OVEN, "Oven"))
    set_state(m, OVEN, "Ready")
    m.apply_available(OVEN, {"data": {"programs": [{"key": "P.A"}]}}, "Ready")
    s.after_response(m, scheduler.Job(kind="available", ha_id=OVEN, state_stamp="Ready"), 200)
    assert m.appliances[OVEN].cache.refreshed_at == clock.wall
    # Five minutes after a first install the daily refresh must not repeat available/commands.
    assert s.daily(m, now_wall=clock.wall + 300) == []
    assert s.daily(m, now_wall=clock.wall + 86_401) != []


def test_commands_answer_stamps_the_daily_refresh_clock():
    m, s, clock = setup((OVEN, "Oven"))
    s.after_response(m, scheduler.Job(kind="commands", ha_id=OVEN), 200)
    assert m.appliances[OVEN].cache.refreshed_at == clock.wall


def test_settings_answer_queues_constraint_fetches_with_control_off():
    m, s, _ = setup((OVEN, "Oven"))
    payload = {"data": {"settings": [{"key": CHILD_LOCK, "value": False}]}}
    m.apply_settings(OVEN, payload, m.tick())
    jobs = s.after_response(m, scheduler.Job(kind="settings", ha_id=OVEN), 200)
    assert [(j.kind, j.key) for j in jobs] == [("setting_detail", CHILD_LOCK)]
    assert jobs[0].budget_class == budget.BACKGROUND
    assert jobs[0].path == f"/api/homeappliances/{OVEN}/settings/{CHILD_LOCK}"


def test_settings_answer_skips_negative_cached_keys():
    m, s, _ = setup((OVEN, "Oven"))
    payload = {"data": {"settings": [{"key": CHILD_LOCK, "value": False}]}}
    m.apply_settings(OVEN, payload, m.tick())
    m.appliances[OVEN].cache.negative_keys.append(CHILD_LOCK)
    assert s.after_response(m, scheduler.Job(kind="settings", ha_id=OVEN), 200) == []


def test_settings_answer_queues_constraint_fetches_once():
    m, s, _ = setup((OVEN, "Oven"))
    payload = {"data": {"settings": [{"key": CHILD_LOCK, "value": False}]}}
    m.apply_settings(OVEN, payload, m.tick())
    jobs = s.after_response(m, scheduler.Job(kind="settings", ha_id=OVEN), 200)
    again = s.after_response(m, scheduler.Job(kind="settings", ha_id=OVEN), 200)
    # Two settings answers before the detail lands coalesce in the worker queue.
    queue = workers.JobQueue(lambda: 0.0)
    for job in jobs + again:
        queue.put(job)
    assert len(queue) == 1
    m.appliances[OVEN].cache.setting_constraints[CHILD_LOCK] = {}
    assert s.after_response(m, scheduler.Job(kind="settings", ha_id=OVEN), 200) == []


def test_running_washer_notifies_cause_no_refetch():
    m, s, _ = setup((WASHER, "Washer"))
    for minutes in range(60):
        items = [
            {"key": "BSH.Common.Option.RemainingProgramTime", "value": 3600 - minutes * 60},
            {"key": "BSH.Common.Option.ProgramProgress", "value": minutes},
            {"key": "LaundryCare.Common.Option.ProcessPhase", "value": "x"},
        ]
        assert s.on_effect(m, m.apply_event(notify(WASHER, items))) == []


def test_program_change_is_settled_coalescable_and_skips_none():
    m, s, clock = setup((OVEN, "Oven"))
    eff = m.apply_event(notify(OVEN, [{"key": "BSH.Common.Root.SelectedProgram", "value": "P.A"}]))
    [job] = s.on_effect(m, eff)
    expected = (job.kind, job.priority, job.dedupe_key)
    assert expected == ("selected", scheduler.PRIORITY_REFETCH, f"{OVEN}:selected")
    assert job.not_before == clock.t + scheduler.PROGRAM_SETTLE_S
    eff = m.apply_event(notify(OVEN, [{"key": "BSH.Common.Root.SelectedProgram", "value": None}]))
    assert s.on_effect(m, eff) == []


def refetch_unknown_setting(m, s, key, answer_keys):
    """Drive the real path: event, queued refetch, then the GET answer applied to the model."""
    eff = m.apply_event(notify(OVEN, [{"key": key, "value": 1}]))
    jobs = s.on_effect(m, eff)
    [job] = jobs
    answer = {"data": {"settings": [{"key": k, "value": 1} for k in answer_keys]}}
    m.apply_settings(OVEN, answer, job.issued_seq)
    s.after_response(m, job, 200)
    return job


def test_unknown_setting_refetch_is_negative_cached():
    m, s, _ = setup((OVEN, "Oven"))
    key = "BSH.Common.Setting.NewThing"
    # The event put the setting in the model, so the first sighting refetches once.
    job = refetch_unknown_setting(m, s, key, [CHILD_LOCK])
    assert (job.kind, job.key) == ("settings", key)
    assert key in m.appliances[OVEN].cache.negative_keys
    eff = m.apply_event(notify(OVEN, [{"key": key, "value": 2}]))
    assert s.on_effect(m, eff) == []


def test_unknown_setting_that_the_refetch_returns_is_not_negative_cached():
    m, s, _ = setup((OVEN, "Oven"))
    key = "BSH.Common.Setting.NewThing"
    refetch_unknown_setting(m, s, key, [key, CHILD_LOCK])
    assert m.appliances[OVEN].cache.negative_keys == []


def test_failed_settings_refetch_does_not_negative_cache():
    m, s, _ = setup((OVEN, "Oven"))
    key = "BSH.Common.Setting.NewThing"
    eff = m.apply_event(notify(OVEN, [{"key": key, "value": 1}]))
    [job] = s.on_effect(m, eff)
    assert s.after_response(m, job, 409) == []
    assert m.appliances[OVEN].cache.negative_keys == []


def test_refetch_cap_per_appliance():
    m, s, clock = setup((OVEN, "Oven"))
    total = 0
    for _ in range(10):
        total += len(s.on_effect(m, m.apply_event(sse.Event("CONNECTED", "", OVEN))))
    assert total == scheduler.CAP_PER_HOUR * 2  # status + settings per allowed refetch
    clock.t += 3601
    assert s.on_effect(m, m.apply_event(sse.Event("CONNECTED", "", OVEN)))


def test_unknown_appliance_triggers_one_list_refetch_per_hour():
    m, s, clock = setup((OVEN, "Oven"))
    eff = m.apply_event(notify("BOSCH-NEW-1", [{"key": "a.b", "value": 1}]))
    assert kinds(s.on_effect(m, eff)) == [("list", "")]
    assert s.on_effect(m, eff) == []
    clock.t += 3601
    assert kinds(s.on_effect(m, eff)) == [("list", "")]


def test_resync_after_reconnect_refreshes_selected_and_active():
    m, s, _ = setup((OVEN, "Oven"), (WASHER, "Washer"), (FRIDGE, "FridgeFreezer"))
    set_state(m, WASHER, "Run")
    jobs = kinds(s.on_stream_reconnected(m))
    assert ("status", OVEN) in jobs and ("selected", OVEN) in jobs
    assert ("active", OVEN) not in jobs
    assert ("selected", WASHER) in jobs and ("active", WASHER) in jobs
    assert ("status", FRIDGE) in jobs
    assert not {"selected", "active"} & {k for k, h in jobs if h == FRIDGE}


def test_daily_refresh_one_idle_appliance_at_a_time_with_missing_constraints():
    m, s, _ = setup((OVEN, "Oven"), (WASHER, "Washer"))
    set_state(m, OVEN, "Ready")
    set_state(m, WASHER, "Run")
    m.appliances[OVEN].settings[CHILD_LOCK] = model.Value(False)
    jobs = s.daily(m, now_wall=200_000.0)
    assert {h for _, h in kinds(jobs)} == {OVEN}
    assert {k for k, _ in kinds(jobs)} == {"available", "commands", "setting_detail"}
    # A cached constraint is never fetched again.
    m.appliances[OVEN].cache.setting_constraints[CHILD_LOCK] = {}
    m.appliances[OVEN].cache.refreshed_at = 0.0
    assert "setting_detail" not in {j.kind for j in s.daily(m, now_wall=200_000.0)}


def test_oauth_jobs_hide_secrets_and_use_the_right_classes():
    job = scheduler.token_poll_job("CID", "DEVICE-CODE", "SECRET")
    assert job.oauth and not job.needs_token and job.budget_class == budget.AUTH
    assert "DEVICE-CODE" not in repr(job) and "SECRET" not in repr(job)
    assert scheduler.refresh_job("R", "").budget_class == budget.AUTH
    assert "client_secret" not in (scheduler.refresh_job("R", "").form or {})
    assert (scheduler.refresh_job("R", "S").form or {})["client_secret"] == "S"


def test_reconnect_then_status_answer_coalesce_program_fetches():
    m, s, _ = setup((WASHER, "Washer"))
    set_state(m, WASHER, "Run")
    m.appliances[WASHER].cache.programs.append("P.A")
    resync = s.on_stream_reconnected(m)
    follow = s.after_response(m, next(j for j in resync if j.kind == "status"), 200)
    for kind in ("selected", "active"):
        keys = [j.dedupe_key for j in resync + follow if j.kind == kind]
        assert keys and set(keys) == {f"{WASHER}:{kind}"}


def test_available_follow_up_selected_shares_the_dedupe_key():
    m, s, _ = setup((OVEN, "Oven"))
    jobs = s.after_response(m, scheduler.Job(kind="available", ha_id=OVEN), 200)
    assert jobs[0].dedupe_key == f"{OVEN}:selected"


def test_discovery_stamps_refresh_clock_when_fetches_are_queued():
    m, s, clock = setup((OVEN, "Oven"))
    set_state(m, OVEN, "Ready")
    s.after_response(m, s.discovery(m, [OVEN])[0], 200)
    assert m.appliances[OVEN].cache.refreshed_at == clock.wall
    assert s.daily(m, now_wall=clock.wall + 5) == []


def test_capped_refetch_is_reported_once_per_window():
    m, s, clock = setup((OVEN, "Oven"))
    for _ in range(scheduler.CAP_PER_HOUR + 4):
        s.on_effect(m, m.apply_event(sse.Event("CONNECTED", "", OVEN)))
    assert s.take_capped() == [OVEN]
    assert s.take_capped() == []
    s.on_effect(m, m.apply_event(sse.Event("CONNECTED", "", OVEN)))
    assert s.take_capped() == []
    clock.t += scheduler.CAPPED_REPORT_S
    # The hourly window reopened, so exhaust it again to be refused and reported anew.
    for _ in range(scheduler.CAP_PER_HOUR + 1):
        s.on_effect(m, m.apply_event(sse.Event("CONNECTED", "", OVEN)))
    assert s.take_capped() == [OVEN]


def test_event_between_refetch_issue_and_answer_keeps_key_out_of_negative_cache():
    m, s, _ = setup((OVEN, "Oven"))
    key = "BSH.Common.Setting.NewThing"
    [job] = s.on_effect(m, m.apply_event(notify(OVEN, [{"key": key, "value": 1}])))
    m.apply_event(notify(OVEN, [{"key": key, "value": 2}]))  # arrives before the answer
    m.apply_settings(OVEN, {"data": {"settings": []}}, job.issued_seq)
    s.after_response(m, job, 200)
    assert m.appliances[OVEN].cache.negative_keys == []


def test_coalesced_refetches_with_different_keys_are_each_judged():
    m, s, _ = setup((OVEN, "Oven"))
    a, b = "BSH.Common.Setting.A", "BSH.Common.Setting.B"
    [job_a] = s.on_effect(m, m.apply_event(notify(OVEN, [{"key": a, "value": 1}])))
    [job_b] = s.on_effect(m, m.apply_event(notify(OVEN, [{"key": b, "value": 1}])))
    # The answer to the first refetch carries A only; the second carries neither.
    m.apply_settings(OVEN, {"data": {"settings": [{"key": a, "value": 1}]}}, job_a.issued_seq)
    s.after_response(m, job_a, 200)
    assert m.appliances[OVEN].cache.negative_keys == []
    m.apply_settings(OVEN, {"data": {"settings": []}}, job_b.issued_seq)
    s.after_response(m, job_b, 200)
    assert m.appliances[OVEN].cache.negative_keys == [b]


def test_daily_retries_available_for_appliance_running_at_first_discovery():
    m, s, clock = setup((WASHER, "Washer"))
    set_state(m, WASHER, "Run")
    s.after_response(m, s.discovery(m, [WASHER])[0], 200)  # commands stamps; available not queued
    assert s.daily(m, now_wall=clock.wall + 10) == []  # still running
    set_state(m, WASHER, "Ready")
    jobs = s.daily(m, now_wall=clock.wall + 20)
    assert ("available", WASHER) in kinds(jobs)
    # An empty answer must not make every heartbeat ask again.
    assert s.daily(m, now_wall=clock.wall + 30) == []
    assert ("available", WASHER) in kinds(s.daily(m, now_wall=clock.wall + 20 + 3600))


def test_failed_available_is_retried_by_the_next_daily():
    m, s, clock = setup((OVEN, "Oven"))
    set_state(m, OVEN, "Ready")
    jobs = s.after_response(m, s.discovery(m, [OVEN])[0], 200)
    m.appliances[OVEN].cache.commands.append("C")
    s.after_response(m, next(j for j in jobs if j.kind == "available"), 409)
    assert s.daily(m, now_wall=clock.wall + 60) == []
    assert ("available", OVEN) in kinds(s.daily(m, now_wall=clock.wall + 3600))


def test_failed_commands_is_retried_by_the_next_daily():
    m, s, clock = setup((OVEN, "Oven"))
    set_state(m, OVEN, "Ready")
    m.appliances[OVEN].cache.programs.append("P.A")
    jobs = s.after_response(m, s.discovery(m, [OVEN])[0], 200)
    s.after_response(m, next(j for j in jobs if j.kind == "commands"), 409)
    assert s.daily(m, now_wall=clock.wall + 60) == []
    assert ("commands", OVEN) in kinds(s.daily(m, now_wall=clock.wall + 3600))


def test_daily_skips_negative_cached_setting_details():
    m, s, _ = setup((OVEN, "Oven"))
    app = m.appliances[OVEN]
    app.settings[CHILD_LOCK] = model.Value(False)
    app.settings["BSH.Common.Setting.Other"] = model.Value(1)
    app.cache.negative_keys.append(CHILD_LOCK)
    keys = [j.key for j in s.daily(m, now_wall=200_000.0) if j.kind == "setting_detail"]
    assert keys == ["BSH.Common.Setting.Other"]


def test_persistent_failure_retries_about_hourly_not_every_heartbeat():
    m, s, clock = setup((OVEN, "Oven"))
    set_state(m, OVEN, "Ready")
    s.after_response(m, s.discovery(m, [OVEN])[0], 200)
    retries = 0
    for _ in range(24 * 60):  # one heartbeat-sized step per minute for a day
        clock.wall += 60
        jobs = s.daily(m, now_wall=clock.wall)
        if jobs:
            retries += 1
            for job in jobs:
                s.after_response(m, job, 409)
    assert 1 <= retries <= 25


def test_two_consecutive_failures_inside_the_window_retry_once():
    m, s, clock = setup((OVEN, "Oven"))
    set_state(m, OVEN, "Ready")
    jobs = s.after_response(m, s.discovery(m, [OVEN])[0], 200)
    for job in jobs:
        s.after_response(m, job, 409)
    s.after_response(m, scheduler.Job(kind="available", ha_id=OVEN), 409)
    assert s.daily(m, now_wall=clock.wall + 60) == []
    assert s.daily(m, now_wall=clock.wall + 1800) == []
    assert s.daily(m, now_wall=clock.wall + 3600) != []


def test_revoke_identifies_the_client():
    job = scheduler.revoke_job("REFRESH-X", "CLIENT-1", "")
    assert job.form == {
        "token": "REFRESH-X",
        "token_type_hint": "refresh_token",
        "client_id": "CLIENT-1",
    }
    assert scheduler.revoke_job("R", "C", "S").form["client_secret"] == "S"
    assert not scheduler.revoke_job("R", "C", "S").needs_token


def test_rest_offline_appliance_back_by_event_is_refetched_once_and_capped():
    m, s, clock = setup((OVEN, "Oven"))
    m.appliances[OVEN].cache.programs.append("P.A")
    m.set_offline(OVEN)
    item = {"key": OP, "value": "BSH.Common.EnumType.OperationState.Run"}
    jobs = s.on_effect(m, m.apply_event(notify(OVEN, [item])))
    assert kinds(jobs) == [("status", OVEN), ("selected", OVEN), ("settings", OVEN)]
    assert {j.priority for j in jobs} == {scheduler.PRIORITY_REFETCH}
    assert s.on_effect(m, m.apply_event(notify(OVEN, [item]))) == []
    # Once connected again, the resync and the daily refresh include it.
    assert ("status", OVEN) in kinds(s.on_stream_reconnected(m))
    # The cap shared by all event-triggered refetches bounds a flapping appliance.
    for _ in range(scheduler.CAP_PER_HOUR):
        m.set_offline(OVEN)
        s.on_effect(m, m.apply_event(notify(OVEN, [item])))
    assert s.take_capped() == [OVEN]


def test_rest_offline_appliance_back_by_a_success_is_refetched():
    m, s, _ = setup((OVEN, "Oven"))
    m.set_offline(OVEN)
    job = scheduler.Job(kind="commands", ha_id=OVEN, issued_seq=m.tick())
    m.apply_answer("commands", OVEN, 200, {"data": {"commands": []}}, issued=job.issued_seq)
    jobs = s.after_response(m, job, 200)
    assert ("status", OVEN) in kinds(jobs) and ("settings", OVEN) in kinds(jobs)
    assert s.after_response(m, job, 200) == []


def test_discovery_covers_a_pending_resync():
    m, s, _ = setup((OVEN, "Oven"))
    m.set_offline(OVEN)
    m.apply_list(
        {"data": {"homeappliances": [{"haId": OVEN, "type": "Oven", "connected": True}]}},
        m.tick(),
    )
    s.discovery(m, [OVEN])
    assert not m.appliances[OVEN].resync_due


def test_daily_includes_an_appliance_back_from_a_rest_offline():
    m, s, _ = setup((OVEN, "Oven"))
    m.set_offline(OVEN)
    assert s.daily(m, now_wall=200_000.0) == []
    item = {"key": OP, "value": "BSH.Common.EnumType.OperationState.Ready"}
    m.apply_event(notify(OVEN, [item]))
    assert ("available", OVEN) in kinds(s.daily(m, now_wall=200_000.0))


def test_missed_connected_then_live_status_refetches_and_rejoins_daily():
    m, s, _ = setup((WASHER, "Washer"))
    m.apply_event(sse.Event("DISCONNECTED", "", WASHER))
    assert s.on_stream_reconnected(m) == []
    item = {"key": OP, "value": "BSH.Common.EnumType.OperationState.Ready"}
    effect = m.apply_event(
        sse.Event("STATUS", json.dumps({"haId": WASHER, "items": [item]}), WASHER)
    )
    assert ("status", WASHER) in kinds(s.on_effect(m, effect))
    assert ("available", WASHER) in kinds(s.daily(m, now_wall=200_000.0))


def test_commands_404_is_not_queued_again_by_later_status_answers():
    m, s, clock = setup((OVEN, "Oven"))
    set_state(m, OVEN, "Ready")
    app = m.appliances[OVEN]
    s.after_response(m, scheduler.Job(kind="commands", ha_id=OVEN), 404)
    status = scheduler.Job(kind="status", ha_id=OVEN)
    assert "commands" not in {j.kind for j in s.after_response(m, status, 200)}
    # Not the hourly retry of a transient failure: the next ask is the daily refresh.
    assert app.cache.refreshed_at == clock.wall


def test_commands_404_keeps_the_hourly_retry_of_a_failed_available():
    m, s, clock = setup((OVEN, "Oven"))
    set_state(m, OVEN, "Ready")
    app = m.appliances[OVEN]
    s.after_response(m, scheduler.Job(kind="available", ha_id=OVEN), 409)
    retry = app.cache.refreshed_at
    assert retry < clock.wall
    s.after_response(m, scheduler.Job(kind="commands", ha_id=OVEN), 404)
    assert app.cache.refreshed_at == retry


def test_reconnect_resync_sends_no_commands_for_appliances_that_offer_none():
    ids = [f"SIEMENS-OVEN-{n}" for n in range(6)]
    m, s, _ = setup(*[(h, "Oven") for h in ids])
    for h in ids:
        set_state(m, h, "Ready")
        s.after_response(m, scheduler.Job(kind="commands", ha_id=h), 404)
    resync = s.on_stream_reconnected(m)
    follow = []
    for job in [j for j in resync if j.kind == "status"]:
        follow += s.after_response(m, job, 200)
    assert "commands" not in {j.kind for j in resync + follow}


def test_daily_refresh_asks_once_for_commands_after_a_404_and_a_200_clears_the_memory():
    m, s, clock = setup((FRIDGE, "FridgeFreezer"))
    s.after_response(m, scheduler.Job(kind="commands", ha_id=FRIDGE), 404)
    assert s.daily(m, now_wall=clock.wall + 60) == []
    asked = [j for j in s.daily(m, now_wall=clock.wall + 86_401) if j.kind == "commands"]
    assert len(asked) == 1
    s.after_response(m, scheduler.Job(kind="commands", ha_id=FRIDGE), 200)
    status = scheduler.Job(kind="status", ha_id=FRIDGE)
    assert "commands" in {j.kind for j in s.after_response(m, status, 200)}
