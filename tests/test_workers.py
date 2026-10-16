import ast
import http.client
import json
import queue
import socket
import threading
import time
from pathlib import Path

import budget
import hc_client
import scheduler
import sse
import workers

HA_ID = "TEST-WASHER-000000000000"


def drain(q, timeout=2.0, until=None):
    out, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            item = q.get(timeout=0.05)
        except queue.Empty:
            if until is not None and until(out):
                break
            continue
        out.append(item)
        if until is not None and until(out):
            break
    return out


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.aborted = False

    def request(self, method, path, **kw):
        self.calls.append((method, path, kw))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def abort(self):
        self.aborted = True


class RecordingBudget(budget.Budget):
    """Pairs every granted acquire with the record that follows it."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.granted = []
        self.recorded = []

    def acquire(self, cls, *, exempt_brake=False):
        decision = super().acquire(cls, exempt_brake=exempt_brake)
        if decision.allowed:
            self.granted.append(decision.probe)
        return decision

    def record(self, *, error, counts=True, probe=False, neutral=False):
        self.recorded.append((error, counts, probe, neutral))
        super().record(error=error, counts=counts, probe=probe, neutral=neutral)


def resp(status, body=b"{}", headers=None):
    return hc_client.Response(status, headers or {}, body, 5)


def api_job(**kw):
    base = dict(
        kind="status",
        path=f"/api/homeappliances/{HA_ID}/status",
        ha_id=HA_ID,
        auth_generation=1,
    )
    base.update(kw)
    return scheduler.Job(**base)


def start_request_worker(client, b=None, mono=time.monotonic):
    results = queue.Queue()
    w = workers.RequestWorker(
        client,
        b or budget.Budget(),
        results,
        generation=3,
        accept_language="nl-NL",
        mono=mono,
        delay_scale=0.001,
    )
    w.set_token("TOKEN", 1)
    w.start()
    return w, results


def results_of(items, cls):
    return [i for i in items if isinstance(i, cls)]


# -- JobQueue --------------------------------------------------------------------------------


def test_job_queue_priority_dedupe_and_not_before():
    t = [100.0]
    q = workers.JobQueue(mono=lambda: t[0])
    q.put(scheduler.Job("status", priority=3, dedupe_key="a"))
    q.put(scheduler.Job("settings", priority=3, dedupe_key="a"))  # replaces the first
    q.put(scheduler.Job("list", priority=0))
    q.put(scheduler.Job("selected", priority=2, not_before=105.0))
    assert q.get(0).kind == "list"
    assert q.get(0).kind == "settings"
    assert q.get(0) is None  # "selected" not due yet
    t[0] = 106.0
    assert q.get(0).kind == "selected"


def test_job_queue_full_drops_background_before_refusing_commands():
    q = workers.JobQueue(maxsize=2)
    assert q.put(scheduler.Job("status", priority=3))
    assert q.put(scheduler.Job("status", priority=0))
    assert q.put(scheduler.Job("x", priority=0))  # evicts the background job
    assert not q.put(scheduler.Job("y", priority=0))  # all left are commands: refused
    assert not q.put(scheduler.Job("z", priority=3))


def test_dedupe_replaces_in_place_and_keeps_the_newest_issued_seq():
    q = workers.JobQueue()
    q.put(scheduler.Job("selected", priority=3, dedupe_key="H:selected", issued_seq=5))
    q.put(scheduler.Job("other", priority=3))
    q.put(scheduler.Job("selected", priority=3, dedupe_key="H:selected", issued_seq=9))
    assert len(q) == 2
    first = q.get(0)
    assert (first.kind, first.issued_seq) == ("selected", 9)  # kept the original slot
    assert q.get(0).kind == "other"


def test_an_older_job_never_replaces_a_newer_one():
    q = workers.JobQueue()
    q.put(scheduler.Job("selected", dedupe_key="k", issued_seq=9))
    assert q.put(scheduler.Job("selected", dedupe_key="k", issued_seq=5, retried=True))
    assert len(q) == 1
    assert q.get(0).issued_seq == 9


def test_token_and_auth_jobs_are_never_coalesced_or_evicted():
    q = workers.JobQueue(maxsize=3)
    assert q.put(scheduler.refresh_job("RT", ""))
    assert q.put(scheduler.refresh_job("RT", ""))
    assert len(q) == 2  # both refreshes kept despite the shared dedupe key
    assert q.put(scheduler.token_poll_job("CID", "DC", ""))
    assert not q.put(scheduler.Job("x", priority=scheduler.PRIORITY_COMMAND))
    assert [q.get(0).kind for _ in range(3)] == ["refresh", "refresh", "token_poll"]


def test_wake_returns_a_waiting_get_early():
    q = workers.JobQueue()
    threading.Timer(0.05, q.wake).start()
    started = time.monotonic()
    assert q.get(5.0) is None
    assert time.monotonic() - started < 2.0


# -- RequestWorker ----------------------------------------------------------------------------


def test_request_executes_and_posts_result_with_language_and_token():
    client = FakeClient([resp(200, b'{"data": {}}')])
    w, results = start_request_worker(client)
    w.submit(api_job())
    items = drain(results, until=lambda o: results_of(o, workers.Result))
    w.stop()
    [r] = results_of(items, workers.Result)
    assert (r.generation, r.status, r.category) == (3, 200, "")
    _, _, kw = client.calls[0]
    assert kw["token"] == "TOKEN" and kw["accept_language"] == "nl-NL"
    assert "body" not in repr(r)


def test_get_retried_once_on_network_error_and_5xx():
    client = FakeClient([hc_client.TransportError("OSError"), resp(503), resp(200)])
    w, results = start_request_worker(client)
    w.submit(api_job())
    items = drain(results, until=lambda o: results_of(o, workers.Result))
    w.stop()
    [r] = results_of(items, workers.Result)
    assert r.status == 503  # network retry used, then the 5xx is not retried a second time
    assert len(client.calls) == 2


def test_get_retried_once_on_408_then_succeeds():
    client = FakeClient([resp(408), resp(200)])
    w, results = start_request_worker(client)
    w.submit(api_job())
    items = drain(results, until=lambda o: results_of(o, workers.Result))
    w.stop()
    [r] = results_of(items, workers.Result)
    assert r.status == 200 and r.job.retried
    assert len(client.calls) == 2


def test_two_network_errors_give_a_network_result():
    client = FakeClient([hc_client.TransportError("OSError"), hc_client.TransportError("timeout")])
    w, results = start_request_worker(client)
    w.submit(api_job())
    items = drain(results, until=lambda o: results_of(o, workers.Result))
    w.stop()
    [r] = results_of(items, workers.Result)
    assert (r.status, r.category) == (0, "network")
    assert len(client.calls) == 2


def test_posts_are_never_retried():
    client = FakeClient([resp(503), resp(200)])
    w, results = start_request_worker(client)
    w.submit(scheduler.device_code_job("CID"))
    items = drain(results, until=lambda o: results_of(o, workers.Result))
    time.sleep(0.05)
    w.stop()
    [r] = results_of(items, workers.Result)
    assert r.status == 503
    assert len(client.calls) == 1


def test_429_blocks_the_budget():
    b = budget.Budget()
    client = FakeClient([resp(429, b'{"error": {"key": "429"}}', {"retry-after": "120"})])
    w, results = start_request_worker(client, b)
    w.submit(api_job())
    drain(results, until=lambda o: results_of(o, workers.Result))
    w.stop()
    assert b.snapshot.blocked_for_s > 100


def test_stale_auth_generation_and_expired_jobs_are_never_sent():
    client = FakeClient([])
    w, results = start_request_worker(client)
    w.set_token("NEW", 2)
    w.submit(api_job(auth_generation=1))
    w.submit(api_job(auth_generation=2, ttl_deadline=time.monotonic() - 1))
    items = drain(results, until=lambda o: len(results_of(o, workers.Refused)) == 2)
    w.stop()
    assert sorted(r.reason for r in results_of(items, workers.Refused)) == ["expired", "stale"]
    assert client.calls == []


def test_parked_worker_refuses_token_jobs_but_runs_oauth():
    client = FakeClient([resp(200, b'{"device_code": "x"}')])
    w, results = start_request_worker(client)
    w.park()
    assert w.state == "parked"
    w.submit(api_job())
    w.submit(scheduler.device_code_job("CID"))
    items = drain(
        results, until=lambda o: results_of(o, workers.Result) and results_of(o, workers.Refused)
    )
    w.stop()
    assert results_of(items, workers.Refused)[0].reason == "no_token"
    assert results_of(items, workers.Result)[0].job.kind == "device_code"
    _, _, kw = client.calls[0]
    assert kw["token"] is None and kw["accept_language"] is None


def test_pending_polls_do_not_trip_the_brake():
    pending = b'{"error": "authorization_pending", "error_description": "x"}'
    client = FakeClient([resp(400, pending) for _ in range(8)])
    b = budget.Budget()
    w, results = start_request_worker(client, b)
    for _ in range(8):
        w.submit(scheduler.token_poll_job("CID", "DC", ""))
    drain(results, until=lambda o: len(results_of(o, workers.Result)) == 8)
    w.stop()
    assert b.snapshot.braked is False


def test_command_refused_when_the_budget_refuses():
    b = budget.Budget()
    b.block(600)
    client = FakeClient([])
    w, results = start_request_worker(client, b)
    w.submit(api_job(priority=scheduler.PRIORITY_COMMAND, budget_class=budget.COMMAND))
    items = drain(results, until=lambda o: results_of(o, workers.Refused))
    w.stop()
    assert results_of(items, workers.Refused)[0].reason == "blocked"
    assert client.calls == []


def test_deeply_nested_error_bodies_never_crash_the_worker():
    deep = b"[" * 200_000
    client = FakeClient([resp(400, deep), resp(400, deep)])
    w, results = start_request_worker(client)
    w.submit(api_job())
    w.submit(scheduler.device_code_job("CID"))
    items = drain(results, until=lambda o: len(results_of(o, workers.Result)) == 2)
    w.stop()
    assert not results_of(items, workers.WorkerCrashed)
    assert sorted(r.error_key for r in results_of(items, workers.Result)) == ["", "unknown"]


def test_crash_is_reported():
    class Boom(FakeClient):
        def request(self, *a, **k):
            raise RuntimeError("bug")

    w, results = start_request_worker(Boom([]))
    w.submit(api_job())
    items = drain(results, until=lambda o: results_of(o, workers.WorkerCrashed))
    w.join(2)
    assert results_of(items, workers.WorkerCrashed)[0].error == "RuntimeError"
    assert w.state == "crashed"
    w.park()
    assert w.state == "crashed"  # only the supervisor replaces a crashed worker


def test_every_granted_request_is_recorded_exactly_once():
    b = RecordingBudget()
    pending = b'{"error": "authorization_pending"}'
    client = FakeClient(
        [
            hc_client.TransportError("OSError"),
            resp(200),
            resp(404, b'{"error": {"key": "SDK.Error.NoProgramActive"}}'),
            resp(400, pending),
            RuntimeError("bug"),
        ]
    )
    w, results = start_request_worker(client, b)
    jobs = [
        api_job(),
        api_job(kind="active"),
        scheduler.token_poll_job("CID", "DC", ""),
    ]
    for job in jobs:  # one at a time, so the responses meet the jobs in a known order
        w.submit(job)
        drain(
            results,
            until=lambda o, k=job.kind: [
                r for r in results_of(o, workers.Result) if r.job.kind == k
            ],
        )
    w.submit(api_job(kind="settings"))
    drain(results, until=lambda o: results_of(o, workers.WorkerCrashed))
    w.join(2)
    assert len(b.granted) == len(b.recorded) == 5
    error, counts, _, _ = b.recorded[0]
    assert (error, counts) == (True, False)  # no HTTP answer: booked, not counted by the brake
    assert b.recorded[1][0] is False
    assert b.recorded[2][:2] == (True, True)  # a state answer counts
    assert b.recorded[3][:2] == (True, False)  # authorization_pending does not
    assert b.recorded[4][:2] == (True, False)  # the crashed request still released its grant


def test_the_brake_probe_slot_is_released_after_a_network_error():
    t = [1000.0]
    b = RecordingBudget(wall=lambda: 1.7e9 + t[0], mono=lambda: t[0])
    for _ in range(budget.BRAKE_PAUSE):
        assert b.acquire(budget.BACKGROUND).allowed
        b.record(error=True)
    b.granted.clear()
    b.recorded.clear()
    t[0] += budget.BRAKE_WINDOW_S + 1
    client = FakeClient([hc_client.TransportError("OSError")])
    w, results = start_request_worker(client, b, mono=lambda: t[0])
    w.submit(api_job())
    drain(results, until=lambda o: len(client.calls) == 1 and b.recorded, timeout=1.0)
    w.stop()
    w.join(2)
    assert b.granted == [True]
    assert b.recorded == [(True, False, True, False)]
    t[0] += budget.BRAKE_WINDOW_S + 1
    assert b.acquire(budget.BACKGROUND).probe  # the slot was freed, so a new probe is granted


def test_logs_carry_no_ha_id_or_token_and_pass_keys_raw():
    key = 'SDK.Error.Weird"\nkey'
    client = FakeClient([resp(409, json.dumps({"error": {"key": key}}).encode())])
    w, results = start_request_worker(client)
    w.set_token("SECRET-TOKEN", 1)
    w.submit(api_job())
    items = drain(results, until=lambda o: results_of(o, workers.Result))
    w.stop()
    logs = results_of(items, workers.LogRecord)
    assert logs
    dumped = repr(logs)
    assert HA_ID not in dumped and "SECRET-TOKEN" not in dumped
    [record] = [r for r in logs if r.event == "request"]
    assert record.fields["key"] == key  # raw; the plugin's emitter quotes and cleans it once


def test_stop_interrupts_a_request_stuck_in_its_read():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    received = threading.Event()

    def serve():
        conn, _ = server.accept()
        conn.recv(4096)
        received.set()
        time.sleep(8)  # never answer: the worker blocks reading the status line
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    client = hc_client.HCClient(
        connect=lambda host, timeout: http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    )
    results = queue.Queue()
    w = workers.RequestWorker(client, budget.Budget(), results, generation=1, accept_language="")
    w.set_token("T", 0)
    w.start()
    w.submit(api_job(auth_generation=0))
    assert received.wait(2)
    started = time.monotonic()
    w.stop()
    w.join(6)
    assert not w.is_alive()
    assert time.monotonic() - started < 2
    server.close()


def test_workers_never_import_domoticz_or_decode_json_themselves():
    tree = ast.parse(Path(workers.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {"DomoticzEx", "Domoticz", "domoticz_api", "json"} & imported


# -- StreamBackoff ----------------------------------------------------------------------------


def test_backoff_grows_caps_and_limits_opens():
    t = [0.0]
    bo = workers.StreamBackoff(mono=lambda: t[0], rand=lambda: 0.5)
    delays = [bo.next_delay() for _ in range(12)]
    assert delays[0] == 10.0 and delays[1] == 20.0 and max(delays) == 3600.0
    bo.reset()
    assert bo.next_delay() == 10.0
    for _ in range(workers.MAX_OPENS_PER_HOUR):
        assert bo.wait_before_open() == 0
        bo.opened()
    assert bo.wait_before_open() == 3600.0


# -- StreamWorker -----------------------------------------------------------------------------


class FakeStream:
    def __init__(self, status, lines, block_at_end=False, headers=None):
        self.status = status
        self.headers = headers or {}
        self.lines = list(lines)
        self.block_at_end = block_at_end
        self.limits = []
        self.closed = False
        self.aborted = threading.Event()

    def readline(self, limit=-1):
        self.limits.append(limit)
        if self.lines:
            return self.lines.pop(0)
        if self.block_at_end:
            # A connection that keeps its socket alive but delivers no complete line.
            self.aborted.wait(5)
            raise hc_client.TransportError("OSError")
        return b""

    def abort(self):
        self.aborted.set()

    def retry_after(self):
        return hc_client.retry_after_seconds(self.headers)

    def close(self):
        self.closed = True


class StreamClient:
    def __init__(self, streams):
        self.streams = list(streams)
        self.opened = 0
        self.open_times = []

    def abort(self):
        pass

    def open_stream(self, token, accept_language=None):
        self.opened += 1
        self.open_times.append(time.monotonic())
        if not self.streams:
            raise hc_client.TransportError("OSError")
        return self.streams.pop(0)


def start_stream_worker(client, b=None, mono=time.monotonic):
    results = queue.Queue()
    w = workers.StreamWorker(
        client,
        b or budget.Budget(),
        results,
        generation=1,
        accept_language="en-GB",
        mono=mono,
        delay_scale=0.001,
    )
    w.set_token("T")
    w.start()
    return w, results


def test_stream_posts_events_but_not_keepalives():
    lines = [
        b"event:KEEP-ALIVE\n",
        b"data:\n",
        b"\n",
        b"event:STATUS\n",
        b'data:{"items":[]}\n',
        b"id:H\n",
        b"\n",
    ]
    stream = FakeStream(200, lines)
    client = StreamClient([stream])
    w, results = start_stream_worker(client)
    items = drain(results, until=lambda o: results_of(o, workers.StreamEvent))
    w.stop()
    w.join(2)
    assert [e.event.type for e in results_of(items, workers.StreamEvent)] == ["STATUS"]
    assert results_of(items, workers.StreamState)[0].connected is True
    assert set(stream.limits) == {sse.MAX_LINE + 1}


def test_stream_401_asks_for_refresh_and_waits_for_a_new_token():
    client = StreamClient([FakeStream(401, [])])
    w, results = start_stream_worker(client)
    items = drain(results, until=lambda o: results_of(o, workers.NeedsRefresh))
    time.sleep(0.1)
    w.stop()
    w.join(2)
    [needs] = results_of(items, workers.NeedsRefresh)
    assert needs.current
    assert client.opened == 1  # no retry with the rejected token


def test_stream_401_for_a_replaced_token_says_so():
    holder = {}

    class SwapClient(StreamClient):
        def open_stream(self, token, accept_language=None):
            holder["w"].set_token("T2")  # a refresh lands while the open with T is in flight
            return super().open_stream(token, accept_language)

    client = SwapClient([FakeStream(401, [])])
    results = queue.Queue()
    w = workers.StreamWorker(
        client, budget.Budget(), results, generation=1, accept_language="en-GB", delay_scale=0.001
    )
    holder["w"] = w
    w.set_token("T")
    w.start()
    items = drain(results, until=lambda o: results_of(o, workers.NeedsRefresh))
    w.stop()
    w.join(2)
    [needs] = results_of(items, workers.NeedsRefresh)
    assert not needs.current


def test_stream_overflow_reconnects_instead_of_crashing():
    huge = b"data:" + b"x" * sse.MAX_LINE + b"\n"
    client = StreamClient([FakeStream(200, [huge]), FakeStream(200, [b"event:STATUS\n", b"\n"])])
    w, results = start_stream_worker(client)
    items = drain(results, until=lambda o: results_of(o, workers.StreamEvent))
    w.stop()
    w.join(2)
    assert not results_of(items, workers.WorkerCrashed)
    reasons = [s.reason for s in results_of(items, workers.StreamState) if not s.connected]
    assert "Overflow" in reasons
    assert client.opened == 2


def test_watchdog_drops_a_stream_that_trickles_lines_without_events():
    t = [0.0]

    class Clocked(FakeStream):
        def readline(self, limit=-1):
            t[0] += 10.0  # each comment line arrives 10 s after the last
            return b": still here\n"

    client = StreamClient([Clocked(200, [])])
    w, results = start_stream_worker(client, mono=lambda: t[0])
    items = drain(results, until=lambda o: any(not s.connected for s in o if hasattr(s, "reason")))
    w.stop()
    w.join(2)
    [closed] = [s for s in results_of(items, workers.StreamState) if not s.connected][:1]
    assert closed.reason == "watchdog"
    assert t[0] >= workers.WATCHDOG_S


def test_keepalives_hold_off_the_watchdog():
    t = [0.0]
    keepalive = [b"event:KEEP-ALIVE\n", b"data:\n", b"\n"]

    class Clocked(FakeStream):
        def readline(self, limit=-1):
            t[0] += 20.0
            if not self.lines:
                return b""
            return self.lines.pop(0)

    client = StreamClient([Clocked(200, keepalive * 4)])
    w, results = start_stream_worker(client, mono=lambda: t[0])
    items = drain(results, until=lambda o: any(not s.connected for s in o if hasattr(s, "reason")))
    w.stop()
    w.join(2)
    [closed] = [s for s in results_of(items, workers.StreamState) if not s.connected][:1]
    assert closed.reason == "closed"  # 240 s on the clock, never 70 s without an event


def test_watchdog_aborts_a_read_that_never_completes_a_line():
    t = [0.0]
    stream = FakeStream(200, [b"event:STATUS\n", b"\n"], block_at_end=True)
    client = StreamClient([stream])
    w, results = start_stream_worker(client, mono=lambda: t[0])
    drain(results, until=lambda o: results_of(o, workers.StreamEvent))
    assert not stream.aborted.is_set()
    t[0] += workers.WATCHDOG_S + 1
    items = drain(results, until=lambda o: any(not s.connected for s in o if hasattr(s, "reason")))
    w.stop()
    w.join(2)
    assert stream.aborted.is_set()
    [closed] = [s for s in results_of(items, workers.StreamState) if not s.connected][:1]
    assert closed.reason == "watchdog"


def test_stream_opens_are_recorded_exactly_once_each():
    b = RecordingBudget()
    client = StreamClient([FakeStream(503, []), FakeStream(200, [b"event:STATUS\n", b"\n"])])
    w, results = start_stream_worker(client, b)
    drain(results, until=lambda o: client.opened >= 3, timeout=3.0)
    w.stop()
    w.join(2)
    assert len(b.granted) == len(b.recorded) >= 3
    assert b.recorded[0][:2] == (True, True)  # 503 answer: counted
    assert b.recorded[1][0] is False  # 200: success
    assert b.recorded[2][:2] == (True, False)  # no HTTP answer: not counted


def test_stop_unblocks_a_stream_blocked_on_a_real_socket():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        conn.recv(4096)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        time.sleep(5)  # then send nothing: the reader blocks
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    client = hc_client.HCClient(
        connect=lambda host, timeout: http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    )
    results = queue.Queue()
    w = workers.StreamWorker(
        client, budget.Budget(), results, generation=1, accept_language="en-GB"
    )
    w.set_token("T")
    w.start()
    drain(results, until=lambda o: results_of(o, workers.StreamState))
    started = time.monotonic()
    w.stop()
    w.join(2)
    assert not w.is_alive()
    assert time.monotonic() - started < 2
    server.close()


def test_stream_429_blocks_the_budget_and_defers_once():
    b = budget.Budget()
    client = StreamClient([FakeStream(429, [])])
    w, results = start_stream_worker(client, b)
    items = drain(
        results,
        until=lambda o: [
            r for r in results_of(o, workers.LogRecord) if r.event == "stream_deferred"
        ],
    )
    time.sleep(0.1)
    w.stop()
    w.join(2)
    items += drain(results, timeout=0.1)
    assert b.snapshot.blocked_for_s > 50
    deferred = [r for r in results_of(items, workers.LogRecord) if r.event == "stream_deferred"]
    assert [r.fields["reason"] for r in deferred] == ["blocked"]
    assert client.opened == 1


def _run_clocked_stream(lines):
    """Run one stream whose lines arrive 20 s apart; return (states, backoff delay after)."""
    t = [0.0]

    class Clocked(FakeStream):
        def readline(self, limit=-1):
            t[0] += 20.0
            return self.lines.pop(0) if self.lines else b""

    results = queue.Queue()
    w = workers.StreamWorker(
        StreamClient([Clocked(200, lines)]),
        budget.Budget(),
        results,
        generation=1,
        accept_language="",
        mono=lambda: t[0],
        delay_scale=0.001,
    )
    w._backoff._delay = 640.0  # as after several failed connects
    w.set_token("T")
    w.start()
    items = drain(
        results,
        until=lambda o: [s for s in results_of(o, workers.StreamState) if not s.connected],
    )
    w.stop()
    w.join(2)
    return results_of(items, workers.StreamState), w._backoff._delay


def test_a_stream_is_healthy_after_five_minutes_with_a_keepalive():
    keepalive = [b"event:KEEP-ALIVE\n", b"data:\n", b"\n"]
    states, delay = _run_clocked_stream(keepalive * 6)
    assert [s for s in states if s.healthy]
    assert delay < 640.0  # reset to the start; without the reset it would be 1280 or more


def test_the_first_event_of_a_connection_marks_the_stream_live_once():
    status = [b"event:EVENT\n", b'data:{"items":[]}\n', b"\n"]
    states, _ = _run_clocked_stream(status * 3)
    live = [s for s in states if s.live]
    assert len(live) == 1
    assert live[0].connected and not live[0].healthy


def test_replayed_events_without_a_keepalive_never_make_a_stream_healthy():
    status = [b"event:EVENT\n", b'data:{"items":[]}\n', b"\n"]
    states, delay = _run_clocked_stream(status * 7)  # 420 s connected
    assert not [s for s in states if s.healthy]
    assert delay >= 640.0  # the backoff was not reset


def test_a_parked_stream_worker_opens_nothing_until_unparked():
    client = StreamClient([FakeStream(200, [b"event:STATUS\n", b"\n"])])
    results = queue.Queue()
    w = workers.StreamWorker(
        client, budget.Budget(), results, generation=1, accept_language="", delay_scale=0.001
    )
    w.set_token("T")
    w.park()
    w.start()
    time.sleep(0.1)
    assert client.opened == 0 and w.state == "parked"
    w.unpark()
    items = drain(results, until=lambda o: results_of(o, workers.StreamEvent))
    w.stop()
    w.join(2)
    assert results_of(items, workers.StreamEvent) and w.state == "running"


def test_a_rest_401_stops_the_queue_from_spending_the_rejected_token():
    b = budget.Budget()
    client = FakeClient([resp(401, b'{"error": {"key": "invalid_token"}}')])
    results = queue.Queue()
    w = workers.RequestWorker(
        client, b, results, generation=1, accept_language="", delay_scale=0.001
    )
    w.set_token("DEAD", 1)
    for i in range(10):
        w.submit(api_job(kind=f"k{i}"))
    w.start()
    items = drain(results, until=lambda o: len(results_of(o, workers.Refused)) == 9)
    assert len(client.calls) == 1
    assert {r.reason for r in results_of(items, workers.Refused)} == {"no_token"}
    [r] = results_of(items, workers.Result)
    assert r.category == "auth"
    client.responses.append(resp(200))
    w.set_token("FRESH", 1)
    w.submit(api_job())
    drain(results, until=lambda o: results_of(o, workers.Result))
    w.stop()
    assert client.calls[-1][2]["token"] == "FRESH"
    assert not b.snapshot.braked


def test_a_refresh_passes_a_brake_pause():
    b = budget.Budget()
    for _ in range(budget.BRAKE_PAUSE):
        b.acquire(budget.BACKGROUND)
        b.record(error=True)
    assert b.snapshot.braked
    client = FakeClient([resp(200, b'{"access_token": "x"}')])
    w, results = start_request_worker(client, b)
    w.submit(scheduler.refresh_job("RT", ""))
    items = drain(results, until=lambda o: results_of(o, workers.Result))
    w.stop()
    assert [r.status for r in results_of(items, workers.Result)] == [200]


def test_an_oauth_success_leaves_the_error_count_alone():
    b = RecordingBudget()
    for _ in range(budget.BRAKE_PAUSE - 1):
        b.acquire(budget.BACKGROUND)
        b.record(error=True)
    client = FakeClient([resp(200, b'{"access_token": "x"}')])
    w, results = start_request_worker(client, b)
    w.submit(scheduler.refresh_job("RT", ""))
    drain(results, until=lambda o: results_of(o, workers.Result))
    w.stop()
    assert b.recorded[-1] == (False, True, False, True)
    assert not b.snapshot.braked
    b.acquire(budget.BACKGROUND)
    b.record(error=True)
    assert b.snapshot.braked  # still 5 before the refresh: one more error makes 6


def test_a_refetch_evicts_background_but_not_another_refetch():
    q = workers.JobQueue(maxsize=2)
    assert q.put(scheduler.Job("bg1", priority=scheduler.PRIORITY_BACKGROUND))
    assert q.put(scheduler.Job("bg2", priority=scheduler.PRIORITY_BACKGROUND))
    admitted = q.put(scheduler.Job("re1", priority=scheduler.PRIORITY_REFETCH))
    assert admitted and admitted.evicted.kind == "bg2"  # the newest background goes first
    assert not q.put(scheduler.Job("bg3", priority=scheduler.PRIORITY_BACKGROUND))
    assert q.put(scheduler.Job("re2", priority=scheduler.PRIORITY_REFETCH)).evicted.kind == "bg1"
    refused = q.put(scheduler.Job("re3", priority=scheduler.PRIORITY_REFETCH))
    assert not refused and refused.evicted is None


def test_an_eviction_is_logged_with_depth_and_oldest_age():
    t = [0.0]
    client = FakeClient([])
    w, results = start_request_worker(client, mono=lambda: t[0])
    for i in range(workers.QUEUE_MAX):
        assert w.submit(api_job(kind=f"bg{i}", not_before=1000.0))
    t[0] = 30.0
    assert w.submit(api_job(kind="re", priority=scheduler.PRIORITY_REFETCH, not_before=1000.0))
    items = drain(results, until=lambda o: results_of(o, workers.LogRecord))
    w.stop()
    [dropped] = [r for r in results_of(items, workers.LogRecord) if r.event == "job_dropped"]
    assert dropped.fields == {
        "kind": f"bg{workers.QUEUE_MAX - 1}",
        "depth": workers.QUEUE_MAX,
        "oldest_age_s": 30.0,
    }


def test_a_job_from_another_auth_generation_is_stale():
    client = FakeClient([])
    w, results = start_request_worker(client)  # auth generation 1
    w.submit(api_job(auth_generation=2))
    items = drain(results, until=lambda o: results_of(o, workers.Refused))
    w.stop()
    assert results_of(items, workers.Refused)[0].reason == "stale"
    assert client.calls == []


def test_a_stream_429_honours_retry_after():
    b = budget.Budget()
    client = StreamClient([FakeStream(429, [], headers={"retry-after": "900"})])
    w, results = start_stream_worker(client, b)
    drain(results, until=lambda o: results_of(o, workers.StreamState))
    w.stop()
    w.join(2)
    assert b.snapshot.blocked_for_s > 800


def test_a_new_token_does_not_cut_the_backoff_short():
    client = StreamClient([FakeStream(401, []), FakeStream(200, [b"event:STATUS\n", b"\n"])])
    results = queue.Queue()
    w = workers.StreamWorker(
        client,
        budget.Budget(),
        results,
        generation=1,
        accept_language="",
        rand=lambda: 0.5,
        delay_scale=0.05,  # the first backoff step, 10 s, becomes 0.5 s
    )
    w.set_token("OLD")
    w.start()
    drain(results, until=lambda o: results_of(o, workers.NeedsRefresh))
    w.set_token("NEW")
    drain(results, until=lambda o: results_of(o, workers.StreamEvent))
    w.stop()
    w.join(2)
    assert client.opened == 2
    assert client.open_times[1] - client.open_times[0] >= 0.4


class _SlowConnect(http.client.HTTPConnection):
    """A connect (or TLS handshake) that takes 3 s, during which abort() cannot reach a socket."""

    def connect(self):
        time.sleep(3.0)
        super().connect()


def _hung_server():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        conn.recv(4096)
        time.sleep(15)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return server, server.getsockname()[1]


def test_stop_during_a_slow_connect_joins_within_the_deadline():
    server, port = _hung_server()
    client = hc_client.HCClient(
        connect=lambda host, timeout: _SlowConnect("127.0.0.1", port, timeout=timeout)
    )
    results = queue.Queue()
    w = workers.RequestWorker(client, budget.Budget(), results, generation=1, accept_language="")
    w.set_token("T", 0)
    w.start()
    w.submit(api_job(auth_generation=0))
    time.sleep(0.5)  # inside the connect
    started = time.monotonic()
    w.stop()
    w.join(6)
    assert not w.is_alive()
    assert time.monotonic() - started < 6
    server.close()


def test_stream_stop_during_a_slow_connect_joins_within_the_deadline():
    server, port = _hung_server()
    client = hc_client.HCClient(
        connect=lambda host, timeout: _SlowConnect("127.0.0.1", port, timeout=timeout)
    )
    results = queue.Queue()
    w = workers.StreamWorker(client, budget.Budget(), results, generation=1, accept_language="")
    w.set_token("T")
    w.start()
    time.sleep(0.5)
    started = time.monotonic()
    w.stop()
    w.join(6)
    assert not w.is_alive()
    assert time.monotonic() - started < 6
    server.close()
