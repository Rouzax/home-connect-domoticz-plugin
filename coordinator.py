"""Per-appliance command coordinator and pending-command overlay (spec 7.6). Pure.

Writes for one appliance go out one at a time, in the order given; appliances take turns. A
Start goes only once every earlier write of its appliance is settled, and never after one that
failed, so a Start never runs on a prerequisite that did not take. Nothing is retried: a write
whose answer never came is reconciled with one GET and reported, never resent.
"""

import itertools
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

import control
import model
import scheduler

QUEUED, IN_FLIGHT, ACCEPTED, OBSERVED = "queued", "in_flight", "accepted", "observed"
FAILED, EXPIRED, CANCELLED, UNKNOWN = "failed", "expired", "cancelled", "unknown_outcome"
_BAD = frozenset({FAILED, EXPIRED, UNKNOWN})
_PENDING = frozenset({QUEUED, IN_FLIGHT, ACCEPTED})
# Writes a Start relies on. A refused Start or Stop is none of them: a Start refused because
# remote start was off must work again as soon as the user has pressed the button.
_PREREQUISITES = frozenset({"select", "option", "setting", "delay"})
_VALUES = frozenset({"option", "setting", "delay"})
CONFIRM_S = 30.0
PREREQUISITE_WINDOW_S = 60.0
SETPOINT_DEBOUNCE_S = 2.0
# A dispatched write whose answer never arrives (its worker died with it) is settled as an
# unknown outcome after this long, and a check whose GET never answers (dropped while a token
# was refreshed, or with a dead worker's queue) is decided on the model. The worker refuses a
# job still unsent at its TTL deadline and sends a write at most once (only GETs are retried),
# so a live answer comes well before.
IN_FLIGHT_MAX_S = 60.0
_EVENTS = {
    OBSERVED: "command_confirmed",
    FAILED: "command_failed",
    EXPIRED: "command_expired",
    CANCELLED: "command_cancelled",
    UNKNOWN: "command_unconfirmed",
}


@dataclass(frozen=True)
class Note:
    """A log line for the plugin to emit, with the appliance named by the plugin."""

    level: str
    event: str
    ha_id: str
    fields: dict[str, object] = field(default_factory=dict)
    # Set only on the confirmation of a delayed-start move: the minutes the plugin holds for the
    # appliance from now on. A move that fails, is refused or stays unconfirmed sets nothing, so
    # the old time stays held and shown (spec 6.3).
    hold: int | None = None


@dataclass
class Write:
    id: int
    intent: control.Intent
    created: float
    due: float
    state: str = QUEUED
    sent_seq: int = 0
    dispatched: float = 0.0
    deadline: float = 0.0
    verify_wanted: bool = False
    verifying: bool = False
    verify_since: float = 0.0
    finished: float = 0.0
    hold: int | None = None
    # The sent Start a Stop given while nothing was seen running depends on.
    after: "Write | None" = None


class Coordinator:
    def __init__(self, lang: str, label: Callable[[str], str]) -> None:
        self._lang = lang
        self._label = label
        self._ids = itertools.count(1)
        self._writes: dict[str, list[Write]] = {}
        self._turn = 0

    # -- intake -------------------------------------------------------------------------------

    def submit(self, intent: control.Intent, now: float, hold: int | None = None) -> list[Note]:
        """Queue one write. `hold` goes with a delayed-start move: the minutes to hold once the
        move is confirmed, reported on that confirmation's Note."""
        queue = self._writes.setdefault(intent.ha_id, [])
        notes: list[Note] = []
        if intent.action == "stop":
            # A Start not sent yet is cancelled; one already sent is reconciled, never assumed
            # cancelled (spec 7.6).
            for queued in queue:
                if queued.state == QUEUED and queued.intent.action == "start":
                    notes.append(self._finish(queued, CANCELLED, now, self._text("cancelled")))
        if intent.request is None:
            return notes
        after = None
        if intent.follows_start:
            after = next((w for w in queue if w.intent.action == "start" and _open(w)), None)
        if intent.coalesce:
            for i, queued in enumerate(queue):
                later = [w for w in queue[i + 1 :] if _open(w)]
                # Never across a Start: it was given for the earlier value, which must stay its
                # prerequisite (spec 7.6). Nor a selection with writes behind it: they were
                # judged against its program.
                kept = any(w.intent.action == "start" for w in later) or (
                    queued.intent.action == "select" and bool(later)
                )
                if (
                    queued.state == QUEUED
                    and queued.intent.coalesce == intent.coalesce
                    and not kept
                ):
                    # The newer value joins the end of the queue: the user's latest command
                    # comes after everything given before it.
                    queued.state, queued.finished = CANCELLED, now
                    fields: dict[str, object] = {"command": queued.intent.describe}
                    notes.append(Note("debug", "command_replaced", intent.ha_id, fields))
        due = now + (SETPOINT_DEBOUNCE_S if intent.debounce else 0.0)
        queue.append(Write(next(self._ids), intent, now, due, hold=hold, after=after))
        notes.append(Note("debug", "command_queued", intent.ha_id, {"command": intent.describe}))
        return notes

    # -- dispatch -----------------------------------------------------------------------------

    def dispatch(self, now: float) -> tuple[list[Write], list[Note]]:
        """The next write of each appliance that may go now, marked in flight."""
        sent: list[Write] = []
        notes: list[Note] = []
        order = sorted(self._writes)
        if order:
            # Rotate which appliance goes first, so none is always last in the worker queue.
            self._turn = (self._turn + 1) % len(order)
            order = order[self._turn :] + order[: self._turn]
        for ha in order:
            queue = self._writes[ha]
            if any(w.state == IN_FLIGHT for w in queue):
                continue
            head = next((w for w in queue if w.state == QUEUED), None)
            if head is None or head.due > now:
                continue
            if now - head.created > control.COMMAND_TTL_S:
                notes.append(self._expire(head, now))
                continue
            if head.intent.action == "start" and not self._start_may_go(queue, head, now, notes):
                continue
            if head.after is not None and not self._stop_may_go(head, head.after, now, notes):
                continue
            head.state, head.dispatched = IN_FLIGHT, now
            sent.append(head)
        return sent, notes

    def _start_may_go(
        self, queue: list[Write], start: Write, now: float, notes: list[Note]
    ) -> bool:
        """A Start carries observed values (spec 7.6), so it waits until every earlier write of
        its appliance is settled; one accepted but not yet seen is checked at once instead of at
        its deadline. One that failed, expired or ended unknown within the last minute refuses
        the Start."""
        earlier = queue[: queue.index(start)]
        waiting = [w for w in earlier if _open(w)]
        if waiting:
            for w in waiting:
                if w.state == ACCEPTED and not w.verifying:
                    w.verify_wanted = True
            return False
        for w in earlier:
            if w.intent.action not in _PREREQUISITES:
                continue
            if w.state in _BAD and now - w.finished <= PREREQUISITE_WINDOW_S:
                reason = self._text("prerequisite", what=w.intent.describe)
                notes.append(self._finish(start, FAILED, now, reason))
                return False
        return True

    def _stop_may_go(self, stop: Write, start: Write, now: float, notes: list[Note]) -> bool:
        """A Stop that follows a sent Start goes once that Start was accepted; one that failed,
        expired, was cancelled or did not take left nothing to stop (spec 7.6), and the DELETE
        would only spend an error response."""
        if start.state in (ACCEPTED, OBSERVED):
            return True
        if _open(start):
            return False
        reason = self._text("nothing_to_stop", a=self._label(stop.intent.ha_id))
        notes.append(self._finish(stop, CANCELLED, now, reason))
        return False

    def sent(self, write: Write, seq: int) -> None:
        """Record the model sequence number the write was sent at: only a newer observation can
        confirm it."""
        write.sent_seq = seq

    def drop(
        self, jobs: Iterable[scheduler.Job | None], now: float, reason_name: str
    ) -> list[Note]:
        """Write jobs the plugin dropped without sending (a new grant, no room to wait for a
        token, a full worker queue): failed with the message `reason_name` names, so a Start
        behind one never runs without it. Other jobs are ignored."""
        reason = self._text(reason_name)
        dropped = (self._find(i) for i in _write_ids(jobs))
        return [self._finish(w, FAILED, now, reason) for w in dropped if w and w.state == IN_FLIGHT]

    def unsent(self, write: Write, now: float, reason: str = "") -> list[Note]:
        """The write never reached the worker: its queue was full, or the Start no longer holds
        when rebuilt for sending."""
        reason = reason or control.refused_reason("queue_full", self._lang)
        return [self._finish(write, FAILED, now, reason)]

    # -- outcomes -----------------------------------------------------------------------------

    def on_answer(
        self,
        write_id: int,
        status: int,
        category: str,
        error_key: str,
        description: str,
        now: float,
        app: model.Appliance | None = None,
    ) -> list[Note]:
        """`app` is the appliance as the model holds it, to name the fix for a state error."""
        write = self._find(write_id)
        if write is None or write.state != IN_FLIGHT:
            return []
        ha = write.intent.ha_id
        if 200 <= status < 300:
            if not write.intent.verify:
                return [self._finish(write, OBSERVED, now)]
            # Accepted is not observed: the model and the device change only on observation.
            write.state, write.deadline = ACCEPTED, now + CONFIRM_S
            write.verify_wanted = write.intent.confirm_by_get
            return [Note("debug", "command_accepted", ha, {"command": write.intent.describe})]
        if status in (0, 408) or status >= 500:
            return self._lost(write, now)
        label = self._label(ha)
        reason = control.answer_reason(
            write.intent, error_key, app, label, self._lang
        ) or control.failure_reason(category, error_key, description, label, self._lang)
        return [self._finish(write, FAILED, now, reason)]

    def _lost(self, write: Write, now: float) -> list[Note]:
        """No usable answer, so the write may have run: never resent; one GET decides."""
        if not write.intent.verify:
            label = self._label(write.intent.ha_id)
            return [self._finish(write, UNKNOWN, now, self._text("unconfirmed", a=label))]
        write.state, write.verify_wanted = UNKNOWN, True
        fields: dict[str, object] = {"command": write.intent.describe}
        return [Note("status", "command_no_answer", write.intent.ha_id, fields)]

    def on_verified(self, write_id: int, app: model.Appliance | None, now: float) -> list[Note]:
        write = self._find(write_id)
        if write is None or not write.verifying:
            return []
        return [self._settle(write, app, now)]

    def _settle(self, write: Write, app: model.Appliance | None, now: float) -> Note:
        """Decide a checked write on what the model holds now."""
        if control.confirmed(write.intent, app, write.sent_seq):
            return self._finish(write, OBSERVED, now)
        name = "start_not_taken" if write.intent.action == "start" else "unconfirmed"
        reason = self._text(name, a=self._label(write.intent.ha_id))
        return self._finish(write, UNKNOWN, now, reason)

    def on_refused(
        self,
        write_id: int,
        reason: str,
        app: model.Appliance | None,
        now: float,
        wait_s: float = 0.0,
    ) -> list[Note]:
        write = self._find(write_id)
        if write is None:
            return []
        if write.verifying:
            # The check itself was not sent: decide on what the model holds now.
            return self.on_verified(write_id, app, now)
        if write.state != IN_FLIGHT:
            return []
        if reason == "expired":
            return [self._expire(write, now)]
        reason = control.refused_reason(reason, self._lang, wait_s)
        return [self._finish(write, FAILED, now, reason)]

    def tick(
        self, apps: Mapping[str, model.Appliance], now: float
    ) -> tuple[list[Note], list[Write]]:
        """Expire, confirm and settle; returns log notes and the writes to check with one GET."""
        notes: list[Note] = []
        verify: list[Write] = []
        for ha, queue in self._writes.items():
            app = apps.get(ha)
            for w in queue:
                if w.state == QUEUED and now - w.created > control.COMMAND_TTL_S:
                    notes.append(self._expire(w, now))
                elif w.state == IN_FLIGHT and now - w.dispatched > IN_FLIGHT_MAX_S:
                    notes += self._lost(w, now)
                elif w.state == ACCEPTED:
                    by_event = not w.intent.confirm_by_get
                    if by_event and control.confirmed(w.intent, app, w.sent_seq):
                        notes.append(self._finish(w, OBSERVED, now))
                    elif now >= w.deadline:
                        w.verify_wanted = True
                if w.verify_wanted and not w.verifying:
                    w.verifying, w.verify_since = True, now
                    verify.append(w)
                elif w.verifying and now - w.verify_since > IN_FLIGHT_MAX_S:
                    notes.append(self._settle(w, app, now))
            self._writes[ha] = [w for w in queue if self._kept(w, now)]
        return notes, verify

    # -- views ---------------------------------------------------------------------------------

    def overlay(self) -> dict[tuple[str, int], tuple[int, str]]:
        """(haId, unit) to the commanded (nvalue, svalue) of every write not settled yet, one
        whose answer was lost included until its reconciliation GET decides; a later write to
        the same unit wins (spec 7.6)."""
        shown: dict[tuple[str, int], tuple[int, str]] = {}
        for ha, queue in self._writes.items():
            for w in queue:
                if _open(w) and w.intent.overlay is not None:
                    unit, nvalue, svalue = w.intent.overlay
                    shown[(ha, unit)] = (nvalue, svalue)
        return shown

    def pending(self, ha_id: str) -> tuple[str | None, bool, bool]:
        """The program of an unconfirmed selection, whether a Start waits to be sent, and
        whether one was sent and is not settled yet (a Stop then may still have something to
        stop)."""
        program, waiting, sent = None, False, False
        for w in self._writes.get(ha_id, []):
            if w.intent.action == "select" and w.state in _PENDING:
                program = w.intent.key
            if w.intent.action == "start" and _open(w):
                if w.state == QUEUED:
                    waiting = True
                else:
                    sent = True
        return program, waiting, sent

    def pending_keys(self, ha_id: str) -> frozenset[str]:
        """Option and setting keys of the appliance with a write not settled yet."""
        writes = self._writes.get(ha_id, [])
        return frozenset(w.intent.key for w in writes if w.intent.action in _VALUES and _open(w))

    def busy(self) -> bool:
        return any(_open(w) for queue in self._writes.values() for w in queue)

    def abandon(
        self,
        now: float,
        reason_name: str = "not_authorized",
        unsent: Iterable[scheduler.Job | None] = (),
    ) -> list[Note]:
        """Authorization is gone or the plugin stops: nothing queued is sent, and queued writes
        are cancelled with the message `reason_name` names, as are the write jobs in `unsent`,
        which are known not to have run (answered 401, held for a refreshed token). Any other write
        not settled, handed to the worker, accepted or waiting for its check, may have run:
        reported unconfirmed, never failed (spec 4.3), since no check follows any more."""
        notes: list[Note] = []
        reason = self._text(reason_name)
        not_run = _write_ids(unsent)
        for ha, queue in self._writes.items():
            for w in queue:
                if w.state == QUEUED or (w.state == IN_FLIGHT and w.id in not_run):
                    notes.append(self._finish(w, CANCELLED, now, reason))
                elif _open(w):
                    unconfirmed = self._text("unconfirmed", a=self._label(ha))
                    notes.append(self._finish(w, UNKNOWN, now, unconfirmed))
        return notes

    # -- internals ----------------------------------------------------------------------------

    def _text(self, name: str, **values: object) -> str:
        return control.message(name, self._lang, **values)

    def _find(self, write_id: int) -> Write | None:
        for queue in self._writes.values():
            for w in queue:
                if w.id == write_id:
                    return w
        return None

    def _expire(self, write: Write, now: float) -> Note:
        # A Stop held behind a Start that never confirmed: the user may still want it stopped.
        name = "expired_stop" if write.after is not None else "expired"
        reason = self._text(name, seconds=int(control.COMMAND_TTL_S))
        return self._finish(write, EXPIRED, now, reason)

    def _finish(self, write: Write, state: str, now: float, reason: str = "") -> Note:
        write.state, write.finished = state, now
        write.verify_wanted = write.verifying = False
        fields: dict[str, object] = {"command": write.intent.describe}
        if reason:
            fields["reason"] = reason
        hold = write.hold if state == OBSERVED else None
        return Note("status", _EVENTS[state], write.intent.ha_id, fields, hold)

    @staticmethod
    def _kept(write: Write, now: float) -> bool:
        """Settled writes stay for the prerequisite window, then go."""
        return _open(write) or now - write.finished <= PREREQUISITE_WINDOW_S


def _write_ids(jobs: Iterable[scheduler.Job | None]) -> set[int]:
    return {j.command_id for j in jobs if j is not None and j.kind == "command"}


def _open(write: Write) -> bool:
    """Not settled: waiting, on its way, accepted but not seen, or waiting for its one check."""
    return write.state in _PENDING or write.verify_wanted or write.verifying
