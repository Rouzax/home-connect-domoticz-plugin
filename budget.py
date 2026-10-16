"""Rate budget for the Home Connect API (spec 7.2, 7.7, 7.8). Pure apart from a lock.

Workers call acquire() immediately before every send and record() after every answer; the
plugin thread only ever reads `snapshot`, which is replaced whole after each change so reading it
needs no lock (spec 4.3).

Persisted values use wall-clock epochs; while running, the 429 block and the ledger age on the
monotonic clock (spec 7.8). A wall-clock jump larger than CLOCK_JUMP_TOLERANCE_S is absorbed by
re-keying the ledger, so a jump in either direction neither erases nor stretches it.
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

AUTH = "auth"
STREAM = "stream"
BACKGROUND = "background"
COMMAND = "command"
CLASSES = (AUTH, STREAM, BACKGROUND, COMMAND)

TOTAL = 950
CEILINGS = {AUTH: 250, STREAM: 40, BACKGROUND: 500}
WINDOW_MINUTES = 24 * 60
PACE_PER_MINUTE = 30
BRAKE_PAUSE = 6
BRAKE_HARD_STOP = 8
BRAKE_WINDOW_S = 600.0
CLOCK_JUMP_TOLERANCE_S = 120.0


def _finite(value: Any) -> float:
    """A persisted number, or 0.0 when it is missing, the wrong type or not finite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0


def _parse_minute(key: Any) -> int | None:
    if isinstance(key, bool) or not isinstance(key, (int, str)):
        return None
    try:
        return int(key)
    except ValueError:
        return None


def _parse_count(count: Any) -> int | None:
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return count


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    wait_s: float = 0.0
    # True when this grant is the single brake probe; pass it on to record(probe=...).
    probe: bool = False


@dataclass(frozen=True)
class BudgetSnapshot:
    used: dict[str, int]
    total_used: int
    blocked_for_s: float  # as of publish time; use block_until_mono for a live comparison
    braked: bool
    exhausted: frozenset[str]
    block_until_mono: float = 0.0  # time.monotonic() value the 429 block ends at, 0.0 if none
    brake_until_mono: float = 0.0  # time.monotonic() value the brake window ends at, 0.0 if none


class Budget:
    def __init__(
        self,
        ledger: Any = None,
        block_until: Any = 0.0,
        block_length: Any = 0.0,
        *,
        wall: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._wall = wall
        self._mono = mono
        now, mono_now = wall(), mono()
        self._offset = now - mono_now
        now_minute = int(now // 60)
        self._ledger: dict[str, dict[int, int]] = {c: {} for c in CLASSES}
        if isinstance(ledger, dict):
            for cls, minutes in ledger.items():
                if cls not in self._ledger or not isinstance(minutes, dict):
                    continue
                for key, count in minutes.items():
                    minute = _parse_minute(key)
                    amount = _parse_count(count)
                    if minute is None or amount is None:
                        continue
                    minute = min(minute, now_minute)  # a future date means the clock went back
                    self._ledger[cls][minute] = self._ledger[cls].get(minute, 0) + amount
        # A clock jump must never stretch a 429 block beyond the length Home Connect asked for.
        self._block_length = max(0.0, _finite(block_length))
        remaining = min(max(0.0, _finite(block_until) - now), self._block_length)
        self._block_until_mono = mono_now + remaining if remaining > 0 else 0.0
        self._recent: deque[float] = deque()
        self._consecutive = 0
        self._last_error_mono = 0.0
        self._probe_in_flight = False
        self._probe_sent_mono = 0.0
        self._snapshot = self._make_snapshot()

    # -- internals, called with the lock held --------------------------------------------------

    def _now(self) -> tuple[float, float]:
        """Read both clocks and absorb any wall-clock jump into the ledger keys."""
        wall, mono = self._wall(), self._mono()
        delta = (wall - mono) - self._offset
        if abs(delta) > CLOCK_JUMP_TOLERANCE_S:
            now_minute = int(wall // 60)
            shift = round(delta / 60) if delta > 0 else 0
            for cls, minutes in self._ledger.items():
                moved: dict[int, int] = {}
                for minute, count in minutes.items():
                    key = min(minute + shift, now_minute)
                    moved[key] = moved.get(key, 0) + count
                self._ledger[cls] = moved
            self._offset = wall - mono
        return wall, mono

    def _used(self, now_minute: int) -> dict[str, int]:
        oldest = now_minute - WINDOW_MINUTES
        used = {}
        for cls, minutes in self._ledger.items():
            for minute in [m for m in minutes if m <= oldest]:
                del minutes[minute]
            used[cls] = sum(minutes.values())
        return used

    def _ceiling(self, cls: str, used: dict[str, int]) -> int:
        if cls == STREAM:
            return CEILINGS[STREAM] + max(0, CEILINGS[BACKGROUND] - used[BACKGROUND])
        if cls == BACKGROUND:
            return CEILINGS[BACKGROUND] - max(0, used[STREAM] - CEILINGS[STREAM])
        if cls == AUTH:
            return CEILINGS[AUTH]
        return TOTAL

    def _brake(self, mono: float, cls: str) -> Decision | None:
        if self._consecutive >= BRAKE_HARD_STOP:
            waited = mono - self._last_error_mono
            if waited < BRAKE_WINDOW_S:
                return Decision(False, "brake", BRAKE_WINDOW_S - waited)
            self._consecutive = 0
            return None
        if self._consecutive >= BRAKE_PAUSE and cls != AUTH:
            # Auth requests (device flow, refresh, revoke) pass a pause: holding refresh back would
            # deadlock an expired access token, since every probe would answer 401. Their error
            # answers still count, except authorization_pending and slow_down, so the hard stop
            # bounds them.
            waited = mono - self._last_error_mono
            if self._probe_in_flight or waited < BRAKE_WINDOW_S:
                return Decision(False, "brake", max(0.0, BRAKE_WINDOW_S - waited))
        return None

    def _check(self, cls: str, exempt_brake: bool = False) -> Decision:
        now, mono = self._now()
        if mono < self._block_until_mono:
            return Decision(False, "blocked", self._block_until_mono - mono)
        braked = None if exempt_brake else self._brake(mono, cls)
        if braked is not None:
            return braked
        used = self._used(int(now // 60))
        if sum(used.values()) >= TOTAL:
            return Decision(False, "total")
        if used[cls] >= self._ceiling(cls, used):
            return Decision(False, "ceiling")
        while self._recent and mono - self._recent[0] >= 60:
            self._recent.popleft()
        if len(self._recent) >= PACE_PER_MINUTE:
            return Decision(False, "pace", 60 - (mono - self._recent[0]))
        return Decision(
            True,
            probe=self._consecutive >= BRAKE_PAUSE and not exempt_brake and cls != AUTH,
        )

    def _make_snapshot(self) -> BudgetSnapshot:
        now, mono = self._now()
        used = self._used(int(now // 60))
        exhausted = frozenset(c for c in CLASSES if used[c] >= self._ceiling(c, used))
        braked = self._consecutive >= BRAKE_PAUSE
        blocked = mono < self._block_until_mono
        return BudgetSnapshot(
            used=used,
            total_used=sum(used.values()),
            blocked_for_s=self._block_until_mono - mono if blocked else 0.0,
            braked=braked,
            exhausted=exhausted,
            block_until_mono=self._block_until_mono if blocked else 0.0,
            brake_until_mono=self._last_error_mono + BRAKE_WINDOW_S if braked else 0.0,
        )

    # -- public -------------------------------------------------------------------------------

    @property
    def snapshot(self) -> BudgetSnapshot:
        return self._snapshot

    def check(self, cls: str) -> Decision:
        with self._lock:
            decision = self._check(cls)
            self._snapshot = self._make_snapshot()
            return decision

    def acquire(self, cls: str, *, exempt_brake: bool = False) -> Decision:
        with self._lock:
            decision = self._check(cls, exempt_brake)
            if decision.allowed:
                now, mono = self._now()
                minute = int(now // 60)
                self._ledger[cls][minute] = self._ledger[cls].get(minute, 0) + 1
                self._recent.append(mono)
                if decision.probe:
                    self._probe_in_flight = True
                    self._probe_sent_mono = mono
            self._snapshot = self._make_snapshot()
            return decision

    def record(
        self, *, error: bool, counts: bool = True, probe: bool = False, neutral: bool = False
    ) -> None:
        """Book an answer. Pass probe=decision.probe so only the probe releases the probe slot.

        neutral=True books an answer that neither feeds nor resets the error count, whatever
        `error` says: an OAuth success proves nothing about the API endpoints the count guards.
        """
        with self._lock:
            mono = self._mono()
            if probe:
                self._probe_in_flight = False
            if neutral:
                if probe:
                    self._last_error_mono = self._probe_sent_mono
            elif not error:
                self._consecutive = 0
            elif counts:
                self._consecutive += 1
                self._last_error_mono = mono
            elif probe:
                # An uncounted answer proves nothing, so wait again from the moment it was sent.
                self._last_error_mono = self._probe_sent_mono
            self._snapshot = self._make_snapshot()

    def block(self, seconds: float) -> None:
        """Start a 429 block of `seconds`; a longer block already running is kept."""
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return
        if not math.isfinite(seconds) or seconds <= 0:
            return
        with self._lock:
            until = self._mono() + seconds
            if until > self._block_until_mono:
                self._block_until_mono = until
                self._block_length = float(seconds)
            self._snapshot = self._make_snapshot()

    def export(self) -> tuple[dict[str, dict[str, int]], float, float]:
        """Ledger (string minutes), block end as a wall epoch and block length."""
        with self._lock:
            wall, mono = self._now()
            ledger = {c: {str(m): n for m, n in mins.items()} for c, mins in self._ledger.items()}
            self._snapshot = self._make_snapshot()  # _now() may have re-keyed the ledger
            if mono < self._block_until_mono:
                return ledger, wall + (self._block_until_mono - mono), self._block_length
            return ledger, 0.0, 0.0
