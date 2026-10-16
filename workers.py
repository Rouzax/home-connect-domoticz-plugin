"""Worker threads owning all network I/O (spec 4.3, 7.9).

Never imports Domoticz: the Domoticz plugin API is not thread-safe, and calling into it from a
thread other than the plugin thread can crash Domoticz. Everything leaves through the results
queue as a message, including log lines, and every message carries the generation of the worker
that produced it so the plugin can discard anything from a worker it has replaced.

The results queue must be unbounded: workers post with put_nowait so they can never block on
the plugin thread, and the plugin drains it every heartbeat (spec 4.3).

Log fields never carry a haId, a path (it contains the haId), a token, a device code or a client
secret. Strings that came from Home Connect go out raw: the plugin's emitter quotes and
cleans every field once (text.log_value), so quoting here would double it.
"""

import queue
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import budget
import errors
import hc_client
import scheduler
import sse

QUEUE_MAX = 64
HEALTHY_AFTER_S = 300.0
BACKOFF_START_S = 10.0
BACKOFF_MAX_S = 3600.0
MAX_OPENS_PER_HOUR = 12
# Reconnect after this long without a complete event, keep-alives included. Above the 55 s
# keep-alive interval and the 65 s socket read timeout; the timeout alone cannot catch a peer
# that trickles bytes, because every byte restarts it.
WATCHDOG_S = 70.0
WATCHDOG_POLL_S = 1.0
RETRY_DELAY_S = 2.0
QUEUE_POLL_S = 0.5
OAUTH = "oauth"
# Device-flow answers that mean "not yet", not failure (spec 8.1).
_POLL_CODES = frozenset({"authorization_pending", "slow_down"})


@dataclass(frozen=True)
class Result:
    generation: int
    job: scheduler.Job
    status: int
    body: bytes = field(repr=False)
    category: str
    error_key: str
    retry_after: float | None
    duration_ms: int


@dataclass(frozen=True)
class Refused:
    generation: int
    job: scheduler.Job
    reason: str


@dataclass(frozen=True)
class StreamEvent:
    generation: int
    event: sse.Event


@dataclass(frozen=True)
class StreamState:
    generation: int
    connected: bool
    healthy: bool
    reason: str
    # The connection delivered its first event, so the data is current again; healthy, which
    # resets the backoff, takes much longer to prove (see _read).
    live: bool = False


@dataclass(frozen=True)
class NeedsRefresh:
    generation: int
    # False when the rejected token had already been replaced while the open was in flight: the
    # newer token is untested, so this 401 says nothing about it.
    current: bool = True


@dataclass(frozen=True)
class LogRecord:
    generation: int
    level: str
    event: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class WorkerCrashed:
    generation: int
    worker: str
    error: str


@dataclass(frozen=True)
class Admission:
    """What JobQueue.put did. Truthy when the job is queued, alone or merged into a queued job
    with the same dedupe key; `evicted` is the lower-priority job dropped to make room."""

    accepted: bool
    evicted: scheduler.Job | None = None
    depth: int = 0
    oldest_age_s: float = 0.0

    def __bool__(self) -> bool:
        return self.accepted


def _never_coalesced(job: scheduler.Job) -> bool:
    """Token and auth jobs are never dropped silently (spec 4.3), so dedupe skips them: each one
    carries its own grant or device code."""
    return job.oauth or job.priority == scheduler.PRIORITY_TOKEN or job.budget_class == budget.AUTH


class JobQueue:
    """Bounded, priority-ordered, deduplicating. A plain queue.Queue is FIFO and unbounded in
    effect for this purpose, so ordering and the overflow policy are explicit here (spec 4.3)."""

    def __init__(self, mono: Callable[[], float] = time.monotonic, maxsize: int = QUEUE_MAX):
        self._mono = mono
        self._max = maxsize
        # (priority, arrival counter, enqueued at, job); the counter keeps equal priorities FIFO.
        self._items: list[tuple[int, int, float, scheduler.Job]] = []
        self._counter = 0
        self._wakes = 0
        self._cond = threading.Condition()

    def __len__(self) -> int:
        with self._cond:
            return len(self._items)

    def _coalesce(self, job: scheduler.Job) -> bool:
        """Replace a queued job with the same dedupe key in its slot; True if one was found."""
        for index, (_, seq, enqueued, queued) in enumerate(self._items):
            if queued.dedupe_key != job.dedupe_key or _never_coalesced(queued):
                continue
            # A requeued retry is older than a job planned since; the newer answer wins.
            if queued.issued_seq <= job.issued_seq:
                self._items[index] = (job.priority, seq, enqueued, job)
                self._cond.notify()
            return True
        return False

    def _admission(self, accepted: bool, evicted: scheduler.Job | None = None) -> Admission:
        oldest = min((i[2] for i in self._items), default=None)
        age = self._mono() - oldest if oldest is not None else 0.0
        return Admission(accepted, evicted, len(self._items), age)

    def put(self, job: scheduler.Job) -> Admission:
        with self._cond:
            if job.dedupe_key and not _never_coalesced(job) and self._coalesce(job):
                return self._admission(True)
            evicted = None
            if len(self._items) >= self._max:
                # Only background and refetch work is re-planned by the scheduler, so only it may
                # be dropped, and only for something more urgent (spec 4.3, 7.7).
                droppable = [i for i in self._items if i[0] >= scheduler.PRIORITY_REFETCH]
                victim = max(droppable, key=lambda i: (i[0], i[1]), default=None)
                if victim is None or victim[0] <= job.priority:
                    return self._admission(False)
                self._items.remove(victim)
                evicted = victim[3]
            self._counter += 1
            self._items.append((job.priority, self._counter, self._mono(), job))
            self._cond.notify()
            return self._admission(True, evicted)

    def get(self, timeout: float) -> scheduler.Job | None:
        """The most urgent due job, or None after `timeout` or a wake()."""
        deadline = self._mono() + timeout
        with self._cond:
            wakes = self._wakes
            while True:
                now = self._mono()
                ready = [i for i in self._items if i[3].not_before <= now]
                if ready:
                    item = min(ready, key=lambda i: (i[0], i[1]))
                    self._items.remove(item)
                    return item[3]
                if now >= deadline or self._wakes != wakes:
                    return None
                waits = [i[3].not_before - now for i in self._items] + [deadline - now]
                self._cond.wait(max(0.01, min(waits)))

    def wake(self) -> None:
        with self._cond:
            self._wakes += 1
            self._cond.notify_all()


class StreamBackoff:
    def __init__(
        self,
        mono: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] = random.random,
    ):
        self._mono = mono
        self._rand = rand
        self._delay = BACKOFF_START_S
        self._opens: list[float] = []

    def next_delay(self) -> float:
        delay = min(BACKOFF_MAX_S, self._delay * (0.8 + 0.4 * self._rand()))
        self._delay = min(BACKOFF_MAX_S, self._delay * 2)
        return delay

    def reset(self) -> None:
        self._delay = BACKOFF_START_S

    def wait_before_open(self) -> float:
        now = self._mono()
        self._opens = [t for t in self._opens if now - t < 3600]
        if len(self._opens) < MAX_OPENS_PER_HOUR:
            return 0.0
        return self._opens[0] + 3600 - now

    def opened(self) -> None:
        self._opens.append(self._mono())


class _Worker(threading.Thread):
    def __init__(
        self,
        name: str,
        client: Any,
        budget_: budget.Budget,
        results: queue.Queue,
        generation: int,
        accept_language: str,
        mono: Callable[[], float],
        delay_scale: float,
    ):
        super().__init__(name=name, daemon=True)
        self._client = client
        self._budget = budget_
        self._results = results
        self.generation = generation
        self._lang = accept_language
        self._mono = mono
        self._scale = delay_scale
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        # Two events so a new token or an unpark ends the wait for a token, while only stop()
        # cuts short a backoff or budget-deferral sleep.
        self._wake = threading.Event()
        self._token_ready = threading.Event()
        self._parked = False
        self.state = "running"

    def _post(self, message: object) -> None:
        self._results.put_nowait(message)

    def _log(self, level: str, event: str, **fields: Any) -> None:
        self._post(LogRecord(self.generation, level, event, fields))

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(max(0.0, seconds) * self._scale)
        self._wake.clear()

    def park(self) -> None:
        with self._lock:
            self._parked = True
            if self.state != "crashed":
                self.state = "parked"

    def unpark(self) -> None:
        with self._lock:
            self._parked = False
            if self.state != "crashed":
                self.state = "running"
        self._token_ready.set()

    def stop(self) -> None:
        """Signal the loop and abort in-flight I/O; returns at once. The caller joins with its
        own deadline (spec 7.9: both workers share one). The client's abort() is permanent, so a
        stopped worker's client is never handed to a new worker."""
        self._stopping.set()
        self._wake.set()
        self._token_ready.set()
        self._client.abort()

    def run(self) -> None:
        try:
            self._loop()
        except Exception as exc:  # reported, then the supervisor decides (spec 7.9)
            with self._lock:
                self.state = "crashed"
            self._post(WorkerCrashed(self.generation, self.name, type(exc).__name__))

    def _loop(self) -> None:
        raise NotImplementedError


class RequestWorker(_Worker):
    def __init__(
        self,
        client: Any,
        budget_: budget.Budget,
        results: queue.Queue,
        *,
        generation: int,
        accept_language: str,
        mono: Callable[[], float] = time.monotonic,
        delay_scale: float = 1.0,
    ):
        super().__init__(
            "hc-request", client, budget_, results, generation, accept_language, mono, delay_scale
        )
        self.jobs = JobQueue(mono)
        self._token: str | None = None
        self._auth_generation = 0

    def set_token(self, token: str | None, auth_generation: int) -> None:
        with self._lock:
            self._token = token
            self._auth_generation = auth_generation

    def submit(self, job: scheduler.Job) -> bool:
        return self._admit(job)

    def stop(self) -> None:
        super().stop()
        self.jobs.wake()

    def _admit(self, job: scheduler.Job) -> bool:
        admission = self.jobs.put(job)
        if admission.evicted is not None:
            self._log(
                "debug",
                "job_dropped",
                kind=admission.evicted.kind,
                depth=admission.depth,
                oldest_age_s=round(admission.oldest_age_s, 1),
            )
        return admission.accepted

    def _loop(self) -> None:
        while not self._stopping.is_set():
            job = self.jobs.get(timeout=QUEUE_POLL_S)
            if job is not None and not self._stopping.is_set():
                self._execute(job)

    def _requeue(self, job: scheduler.Job, wait_s: float) -> None:
        if not self._admit(replace(job, not_before=self._mono() + wait_s * self._scale)):
            self._post(Refused(self.generation, job, "queue_full"))

    def _refuse(self, job: scheduler.Job, reason: str) -> None:
        self._log("debug", "job_refused", kind=job.kind, reason=reason)
        self._post(Refused(self.generation, job, reason))

    def _execute(self, job: scheduler.Job) -> None:
        if job.ttl_deadline and self._mono() > job.ttl_deadline:
            self._refuse(job, "expired")
            return
        with self._lock:
            token, auth_generation, parked = self._token, self._auth_generation, self._parked
        if job.needs_token:
            if parked or token is None:
                self._refuse(job, "no_token")
                return
            if job.auth_generation != auth_generation:
                self._refuse(job, "stale")
                return
        decision = self._budget.acquire(job.budget_class, exempt_brake=job.kind == "revoke")
        if not decision.allowed:
            if job.priority == scheduler.PRIORITY_COMMAND or decision.reason in (
                "total",
                "ceiling",
            ):
                self._refuse(job, decision.reason)
            else:
                self._requeue(job, max(decision.wait_s, 1.0))
            return
        self._send(job, token if job.needs_token else None, decision)

    def _classify(self, job: scheduler.Job, resp: hc_client.Response) -> tuple[str, str, bool]:
        """(category, error key, counts toward the brake) for an HTTP answer."""
        if resp.status < 400:
            return "", "", True
        if job.oauth:
            code = errors.parse_oauth_error(resp.status, resp.body).code
            category = errors.LIMITED if resp.status == 429 else OAUTH
            return category, code, code not in _POLL_CODES
        api = errors.parse_api_error(resp.status, resp.body)
        return api.category, api.key or "", errors.counts_for_brake(api.category)

    def _send(self, job: scheduler.Job, token: str | None, decision: budget.Decision) -> None:
        """One attempt, booked with exactly one budget.record() for the grant in `decision`."""
        started = self._mono()
        resp: hc_client.Response | None = None
        failure = ""
        category, key = "", ""
        # The booking if anything below raises: an uncounted error, which still releases the
        # grant and the brake probe slot before the crash propagates.
        error, counts, neutral = True, False, False
        try:
            try:
                answer: hc_client.Response = self._client.request(
                    job.method,
                    job.path,
                    token=token,
                    form=job.form,
                    accept_language=None if job.oauth else self._lang,
                )
            except hc_client.TransportError as exc:
                # No HTTP answer: booked in the ledger at acquire, never counted by the brake.
                failure = str(exc)
                counts = errors.counts_for_brake(errors.NETWORK)
            else:
                resp = answer
                category, key, counts = self._classify(job, answer)
                error = answer.status >= 400
                # An OAuth success says nothing about the API endpoints the count guards.
                neutral = job.oauth and not error
        finally:
            self._budget.record(error=error, counts=counts, neutral=neutral, probe=decision.probe)
        if resp is None:
            retry = job.method == "GET" and not job.retried
            self._log(
                "debug",
                "request",
                kind=job.kind,
                error=failure,
                duration_ms=int((self._mono() - started) * 1000),
                outcome="retry" if retry else "error",
            )
            if retry:
                self._requeue(replace(job, retried=True), RETRY_DELAY_S)
            else:
                self._post(Result(self.generation, job, 0, b"", errors.NETWORK, "", None, 0))
            return
        if resp.status == 429:
            self._budget.block(resp.retry_after() or 60.0)
        if category == errors.AUTH and token is not None:
            # Every queued job would carry the same rejected token and each 401 feeds the brake,
            # so the rest wait as "no_token" until set_token delivers a refreshed one.
            with self._lock:
                if self._token == token:
                    self._token = None
        retry = (
            job.method == "GET" and not job.retried and (resp.status >= 500 or resp.status == 408)
        )
        fields: dict[str, Any] = {"kind": job.kind, "status": resp.status}
        if key:
            fields["key"] = key
        self._log(
            "debug",
            "request",
            **fields,
            duration_ms=resp.duration_ms,
            outcome="retry" if retry else ("success" if resp.status < 400 else "error"),
        )
        if retry:
            self._requeue(replace(job, retried=True), RETRY_DELAY_S)
            return
        self._post(
            Result(
                self.generation,
                job,
                resp.status,
                resp.body,
                category,
                key,
                resp.retry_after(),
                resp.duration_ms,
            )
        )


class StreamWorker(_Worker):
    def __init__(
        self,
        client: Any,
        budget_: budget.Budget,
        results: queue.Queue,
        *,
        generation: int,
        accept_language: str,
        mono: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] = random.random,
        delay_scale: float = 1.0,
    ):
        super().__init__(
            "hc-stream", client, budget_, results, generation, accept_language, mono, delay_scale
        )
        self._backoff = StreamBackoff(mono, rand)
        self._token: str | None = None
        self._stream: hc_client.Stream | None = None
        self._deferred = ""

    def set_token(self, token: str | None) -> None:
        with self._lock:
            self._token = token
        self._token_ready.set()

    def stop(self) -> None:
        super().stop()
        with self._lock:
            stream = self._stream
        if stream is not None:
            stream.abort()

    def _loop(self) -> None:
        while not self._stopping.is_set():
            with self._lock:
                token, parked = self._token, self._parked
            if parked or token is None:
                self._token_ready.wait(1.0)
                self._token_ready.clear()
                continue
            wait = self._backoff.wait_before_open()
            if wait > 0:
                self._sleep(wait)
                continue
            if self._stopping.is_set():
                break
            decision = self._budget.acquire(budget.STREAM)
            if not decision.allowed:
                if decision.reason != self._deferred:  # latched: logged once per change
                    self._deferred = decision.reason
                    self._log("debug", "stream_deferred", reason=decision.reason)
                self._sleep(max(decision.wait_s, 30.0))
                continue
            self._deferred = ""
            self._backoff.opened()
            self._run_once(token, decision)
            if not self._stopping.is_set():
                self._sleep(self._backoff.next_delay())

    def _open(self, token: str, decision: budget.Decision) -> hc_client.Stream | None:
        """Open the stream and book the attempt with exactly one budget.record()."""
        stream: hc_client.Stream | None = None
        failure = ""
        error, counts = True, False  # the booking if open_stream raises anything unexpected
        try:
            try:
                opened: hc_client.Stream = self._client.open_stream(token, self._lang)
            except hc_client.TransportError as exc:
                failure = str(exc)
                counts = errors.counts_for_brake(errors.NETWORK)
            else:
                stream = opened
                error, counts = opened.status != 200, True
        finally:
            self._budget.record(error=error, counts=counts, probe=decision.probe)
        if stream is None:
            self._log("debug", "stream_open", error=failure, outcome="error")
            self._post(StreamState(self.generation, False, False, failure))
            return None
        if stream.status == 200:
            return stream
        stream.close()
        if stream.status == 401:
            with self._lock:
                current = self._token == token
                if current:
                    self._token = None  # never retry a token Home Connect just rejected
            self._post(NeedsRefresh(self.generation, current))
        elif stream.status == 429:
            self._budget.block(stream.retry_after() or 60.0)
        self._log("debug", "stream_open", status=stream.status, outcome="error")
        self._post(StreamState(self.generation, False, False, f"http_{stream.status}"))
        return None

    def _run_once(self, token: str, decision: budget.Decision) -> None:
        stream = self._open(token, decision)
        if stream is None:
            return
        with self._lock:
            self._stream = stream
        # stop() sets the flag before it reads _stream, so either it saw this stream and aborted
        # it, or the flag is visible here.
        if self._stopping.is_set():
            with self._lock:
                self._stream = None
            stream.close()
            return
        self._log("debug", "stream_open", status=200, outcome="success")
        self._post(StreamState(self.generation, True, False, "connected"))
        reason = self._read(stream)
        self._log("debug", "stream_closed", reason=reason)
        self._post(StreamState(self.generation, False, False, reason))

    def _read(self, stream: hc_client.Stream) -> str:
        """Read events until the stream ends; returns why it ended."""
        connected_at = self._mono()
        last_event = [connected_at]
        done, fired = threading.Event(), threading.Event()

        def watchdog() -> None:
            # readline() cannot be interrupted from its own thread, so a stalled read is ended
            # from here through the same socket shutdown stop() uses.
            while not done.wait(max(0.01, WATCHDOG_POLL_S * self._scale)):
                if self._mono() - last_event[0] >= WATCHDOG_S:
                    fired.set()
                    stream.abort()
                    return

        threading.Thread(target=watchdog, name="hc-stream-watchdog", daemon=True).start()
        keepalives, live, healthy, reason = 0, False, False, "closed"
        parser = sse.Parser()
        try:
            while not self._stopping.is_set():
                line = stream.readline(sse.MAX_LINE + 1)
                if not line:
                    break
                event = parser.feed(line)
                now = self._mono()
                if event is None:
                    if now - last_event[0] >= WATCHDOG_S:
                        fired.set()
                        break
                    continue
                last_event[0] = now
                if not live:
                    live = True
                    self._post(StreamState(self.generation, True, False, "live", live=True))
                if event.type == "KEEP-ALIVE":
                    keepalives += 1
                else:
                    self._post(StreamEvent(self.generation, event))
                # Replayed present events arrive on every connect, so only time connected plus a
                # keep-alive proves the stream healthy enough to reset the backoff (spec 7.7).
                if not healthy and keepalives and now - connected_at >= HEALTHY_AFTER_S:
                    healthy = True
                    self._backoff.reset()
                    self._post(StreamState(self.generation, True, True, "healthy"))
        except hc_client.TransportError as exc:
            reason = str(exc)
        except sse.Overflow:
            reason = "Overflow"
        finally:
            done.set()
            with self._lock:
                self._stream = None
            stream.close()
        if fired.is_set():
            return "watchdog"
        if self._stopping.is_set():
            return "stopped"
        return reason
