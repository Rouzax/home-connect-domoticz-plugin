import threading

import pytest

import budget


class Clock:
    def __init__(self, wall=1_800_000_000.0, mono=1000.0):
        self.w, self.m = wall, mono

    def advance(self, s):
        self.w += s
        self.m += s


def make(clock, **kw):
    return budget.Budget(wall=lambda: clock.w, mono=lambda: clock.m, **kw)


def spend(b, clock, cls, n, step=2.1):
    for _ in range(n):
        d = b.acquire(cls)
        assert d.allowed, d
        b.record(error=False)
        clock.advance(step)


def test_background_ceiling():
    c = Clock()
    b = make(c)
    spend(b, c, budget.BACKGROUND, 500)
    assert b.acquire(budget.BACKGROUND).reason == "ceiling"
    assert b.acquire(budget.COMMAND).allowed


def test_stream_borrows_unused_background():
    c = Clock()
    b = make(c)
    spend(b, c, budget.STREAM, 60)  # beyond its 40, borrowing from background
    snap = b.snapshot
    assert snap.used[budget.STREAM] == 60
    spend(b, c, budget.BACKGROUND, 480)
    assert b.acquire(budget.BACKGROUND).reason == "ceiling"  # 500 minus the 20 borrowed


def test_total_cap_and_rolling_window():
    c = Clock()
    b = make(c)
    spend(b, c, budget.AUTH, 250)
    spend(b, c, budget.BACKGROUND, 500)
    spend(b, c, budget.STREAM, 40)
    spend(b, c, budget.COMMAND, 90)
    assert b.acquire(budget.COMMAND).reason == "ceiling"
    c.advance(86_400)
    assert b.acquire(budget.COMMAND).allowed


def test_pacing_per_minute():
    c = Clock()
    b = make(c)
    for _ in range(30):
        assert b.acquire(budget.COMMAND).allowed
    d = b.acquire(budget.COMMAND)
    assert (d.allowed, d.reason) == (False, "pace")
    c.advance(61)
    assert b.acquire(budget.COMMAND).allowed


def test_brake_pauses_probes_and_hard_stops():
    c = Clock()
    b = make(c)
    for _ in range(6):
        assert b.acquire(budget.BACKGROUND).allowed
        b.record(error=True)
        c.advance(3)
    assert b.acquire(budget.COMMAND).reason == "brake"
    c.advance(600)
    probe = b.acquire(budget.BACKGROUND)  # the probe
    assert probe.allowed and probe.probe
    assert b.acquire(budget.BACKGROUND).reason == "brake"  # only one probe in flight
    b.record(error=True, probe=True)  # probe failed: 7
    c.advance(600)
    assert b.acquire(budget.BACKGROUND).allowed
    b.record(error=True, probe=True)  # 8: hard stop
    c.advance(599)
    assert b.acquire(budget.BACKGROUND).reason == "brake"
    c.advance(2)
    assert b.acquire(budget.BACKGROUND).allowed  # window passed, reset


def test_revoke_may_pass_the_brake_but_not_a_block():
    c = Clock()
    b = make(c)
    for _ in range(6):
        b.acquire(budget.BACKGROUND)
        b.record(error=True)
    assert b.acquire(budget.COMMAND).reason == "brake"
    assert b.acquire(budget.COMMAND, exempt_brake=True).allowed
    b.block(60)
    assert b.acquire(budget.AUTH, exempt_brake=True).reason == "blocked"


def test_success_resets_brake_and_uncounted_errors_do_nothing():
    c = Clock()
    b = make(c)
    for _ in range(5):
        b.acquire(budget.BACKGROUND)
        b.record(error=True)
    b.record(error=False)
    for _ in range(5):
        b.acquire(budget.AUTH)
        b.record(error=True, counts=False)  # authorization_pending polls
    assert b.acquire(budget.BACKGROUND).allowed
    assert b.snapshot.braked is False


def test_block_window_and_clamp_on_load():
    c = Clock()
    b = make(c)
    b.block(120)
    d = b.acquire(budget.COMMAND)
    assert d.reason == "blocked"
    assert d.wait_s == pytest.approx(120)
    ledger, until, length = b.export()
    c.w -= 10_000  # wall clock jumped backwards
    b2 = make(c, ledger=ledger, block_until=until, block_length=length)
    d2 = b2.check(budget.COMMAND)
    assert d2.reason == "blocked"
    assert d2.wait_s == pytest.approx(120)


def test_future_dated_ledger_entries_are_clamped_not_lost():
    c = Clock()
    future_minute = str(int(c.w // 60) + 5000)
    b = make(c, ledger={budget.BACKGROUND: {future_minute: 500}})
    assert b.acquire(budget.BACKGROUND).reason == "ceiling"


def test_export_round_trip_uses_string_minutes():
    c = Clock()
    b = make(c)
    spend(b, c, budget.COMMAND, 3)
    ledger, _, _ = b.export()
    assert all(isinstance(k, str) for k in ledger[budget.COMMAND])
    assert sum(ledger[budget.COMMAND].values()) == 3


def test_malformed_persisted_values_are_treated_as_absent():
    c = Clock()
    nan, inf = float("nan"), float("inf")
    bad = {
        budget.BACKGROUND: {
            "x": 5,
            "1e999": 5,
            str(int(c.w // 60)): nan,
            "7": inf,
            "8": "9",
            "9": -4,
        },
        budget.COMMAND: "not a dict",
        "bogus": {"1": 1},
    }
    b = make(c, ledger=bad, block_until=nan, block_length=inf)
    assert b.snapshot.total_used == 0
    assert b.acquire(budget.COMMAND).allowed
    for until, length in [("soon", None), (inf, 60.0), (c.w + 500, nan), (c.w + 500, -5.0)]:
        assert make(c, block_until=until, block_length=length).check(budget.COMMAND).allowed
    assert make(c, ledger=[1, 2]).snapshot.total_used == 0


def test_snapshot_is_replaced_not_mutated():
    c = Clock()
    b = make(c)
    before = b.snapshot
    b.acquire(budget.COMMAND)
    after = b.snapshot
    assert after is not before
    assert before.total_used == 0
    assert after.total_used == 1


def test_block_survives_wall_jumps_while_running():
    c = Clock()
    b = make(c)
    b.block(60)
    c.w -= 3600  # wall clock steps back an hour
    d = b.check(budget.COMMAND)
    assert d.reason == "blocked" and d.wait_s == pytest.approx(60)
    c.m += 30
    assert b.check(budget.COMMAND).wait_s == pytest.approx(30)
    c.w += 7200  # and forward two hours: the block is still live
    assert b.check(budget.COMMAND).reason == "blocked"
    c.m += 31
    c.w += 31
    assert b.check(budget.COMMAND).allowed


def test_export_converts_block_back_to_wall_time():
    c = Clock()
    b = make(c)
    b.block(100)
    c.m += 40
    c.w += 40 + 5000  # jump forward
    _, until, length = b.export()
    assert until == pytest.approx(c.w + 60)
    assert length == 100


def test_expired_block_exports_as_none():
    c = Clock()
    b = make(c)
    b.block(10)
    c.advance(11)
    assert b.export()[1:] == (0.0, 0.0)


def test_forward_wall_jump_does_not_wipe_ledger():
    c = Clock()
    b = make(c)
    spend(b, c, budget.BACKGROUND, 500)
    c.w += 2 * 86_400
    assert b.acquire(budget.BACKGROUND).reason == "ceiling"
    assert b.snapshot.used[budget.BACKGROUND] == 500
    c.advance(86_400 + 60)  # real elapsed time ages it out
    assert b.acquire(budget.BACKGROUND).allowed


def test_backward_wall_jump_does_not_keep_entries_in_the_future():
    c = Clock()
    b = make(c)
    spend(b, c, budget.BACKGROUND, 500)
    c.w -= 3 * 86_400
    assert b.acquire(budget.BACKGROUND).reason == "ceiling"
    c.advance(86_400 + 60)
    assert b.acquire(budget.BACKGROUND).allowed


def test_small_wall_drift_is_ignored():
    c = Clock()
    b = make(c)
    spend(b, c, budget.COMMAND, 3)
    c.w += 60
    ledger, _, _ = b.export()
    assert sum(ledger[budget.COMMAND].values()) == 3
    assert b.snapshot.used[budget.COMMAND] == 3


def _paused(c):
    b = make(c)
    for _ in range(6):
        b.acquire(budget.BACKGROUND)
        b.record(error=True)
    return b


def test_revoke_during_probe_does_not_release_a_second_probe():
    c = Clock()
    b = _paused(c)
    c.advance(601)
    probe = b.acquire(budget.BACKGROUND)
    assert probe.probe
    revoke = b.acquire(budget.AUTH, exempt_brake=True)
    assert revoke.allowed and not revoke.probe
    b.record(error=True, counts=False)  # revoke answered; the probe is still on the wire
    assert b.acquire(budget.BACKGROUND).reason == "brake"


def test_auth_passes_a_brake_pause_without_taking_the_probe():
    c = Clock()
    b = _paused(c)
    d = b.acquire(budget.AUTH)
    assert d.allowed and not d.probe
    c.advance(601)
    d = b.acquire(budget.AUTH)
    assert d.allowed and not d.probe
    assert b.acquire(budget.BACKGROUND).probe  # the probe slot was not consumed


def test_auth_is_refused_at_hard_stop_but_revoke_passes():
    c = Clock()
    b = make(c)
    for _ in range(8):
        b.acquire(budget.BACKGROUND)
        b.record(error=True)
    assert b.acquire(budget.AUTH).reason == "brake"
    assert b.acquire(budget.AUTH, exempt_brake=True).allowed


def test_uncounted_probe_answer_waits_ten_more_minutes_from_send():
    c = Clock()
    b = _paused(c)
    c.advance(601)
    assert b.acquire(budget.BACKGROUND).probe
    c.advance(5)
    b.record(error=True, counts=False, probe=True)
    d = b.acquire(budget.BACKGROUND)
    assert d.reason == "brake" and d.wait_s == pytest.approx(600 - 5)
    c.advance(596)
    assert b.acquire(budget.BACKGROUND).probe


def test_snapshot_reflects_hard_stop_expiry_after_a_check():
    c = Clock()
    b = make(c)
    for _ in range(8):
        b.acquire(budget.BACKGROUND)
        b.record(error=True)
    snap = b.snapshot
    assert snap.braked and snap.brake_until_mono == pytest.approx(c.m + 600)
    c.advance(601)
    assert b.snapshot.braked  # nothing has run since
    b.check(budget.BACKGROUND)
    assert b.snapshot.braked is False
    assert b.snapshot.brake_until_mono == 0.0


def test_snapshot_publishes_refusals_and_block_deadline():
    c = Clock()
    b = make(c)
    b.block(90)
    snap = b.snapshot
    assert snap.block_until_mono == pytest.approx(c.m + 90)
    assert snap.blocked_for_s == pytest.approx(90)
    c.advance(91)
    b.check(budget.COMMAND)
    assert b.snapshot.blocked_for_s == 0.0
    assert b.snapshot.block_until_mono == 0.0


def test_block_keeps_longer_block_and_matching_length_and_rejects_bad_input():
    c = Clock()
    b = make(c)
    b.block(100)
    b.block(10)
    assert b.export()[2] == 100
    for bad in (float("nan"), float("inf"), -5, 0, "x", True):
        b.block(bad)  # type: ignore[arg-type]
    assert b.export()[2] == 100
    assert b.check(budget.COMMAND).wait_s == pytest.approx(100)


def _contended_budget(c):
    """A Budget whose clock moves inside the monotonic reads it makes under its own lock.

    Time then advances atomically with every attempt, refusals included, so scheduling can never
    bunch grants inside one pacing minute.
    """

    def mono():
        c.advance(1.0)
        return c.m

    return budget.Budget(wall=lambda: c.w, mono=mono)


def _hammer(b, classes, per_class=3):
    """Run threads per class until each has been refused 20 times in a row."""
    errors: list[BaseException] = []

    def work(cls):
        try:
            misses = 0
            while misses < 20:
                d = b.acquire(cls)
                if d.allowed:
                    misses = 0
                    b.record(error=False)
                else:
                    assert d.reason in ("ceiling", "total"), d
                    misses += 1
        except BaseException as exc:  # surfaced by the assert below, not as a thread warning
            errors.append(exc)

    threads = [
        threading.Thread(target=work, args=(cls,)) for cls in classes for _ in range(per_class)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []


def test_threads_hit_ceilings_and_total_exactly():
    c = Clock()
    b = _contended_budget(c)
    _hammer(b, [budget.AUTH, budget.STREAM, budget.BACKGROUND])
    used = b.snapshot.used
    assert used[budget.AUTH] == budget.CEILINGS[budget.AUTH]
    # Stream borrows unused background budget, so the pair shares exactly their combined ceilings.
    assert used[budget.STREAM] >= budget.CEILINGS[budget.STREAM]
    assert used[budget.STREAM] + used[budget.BACKGROUND] == 540
    _hammer(b, [budget.COMMAND])
    snap = b.snapshot
    assert c.m - 1000.0 < 86_400
    assert snap.total_used == budget.TOTAL - budget.COMMAND_RESERVE
    assert snap.used[budget.COMMAND] == budget.TOTAL - budget.COMMAND_RESERVE - 250 - 540
    assert snap.used[budget.AUTH] == 250


def test_a_neutral_answer_neither_resets_nor_feeds_the_count():
    c = Clock()
    b = make(c)
    for _ in range(budget.BRAKE_PAUSE - 1):
        assert b.acquire(budget.BACKGROUND).allowed
        b.record(error=True)
    assert b.acquire(budget.AUTH).allowed
    b.record(error=False, neutral=True)  # a token refresh that succeeded
    assert not b.snapshot.braked
    assert b.acquire(budget.BACKGROUND).allowed
    b.record(error=True)
    assert b.snapshot.braked  # still 5 before the refresh, so this error makes 6


def test_a_neutral_probe_answer_releases_the_slot_and_restarts_the_wait():
    c = Clock()
    b = _paused(c)
    c.advance(601)
    d = b.acquire(budget.BACKGROUND)
    assert d.probe
    c.advance(5)
    b.record(error=False, neutral=True, probe=True)
    assert b.snapshot.braked
    c.advance(590)
    assert b.acquire(budget.BACKGROUND).reason == "brake"  # 600 s from the probe's send
    c.advance(10)
    assert b.acquire(budget.BACKGROUND).probe


def test_starts_and_stops_are_limited_to_five_a_minute_each():
    c = Clock()
    b = make(c)
    for _ in range(budget.ACTION_LIMIT):
        assert b.acquire(budget.COMMAND, limit="start").allowed
        b.record(error=False)
        c.advance(1)
    d = b.acquire(budget.COMMAND, limit="start")
    assert (d.allowed, d.reason) == (False, "limiter")
    assert d.wait_s == pytest.approx(55)
    assert b.acquire(budget.COMMAND, limit="stop").allowed  # its own counter
    assert b.acquire(budget.COMMAND).allowed  # other commands are not limited
    c.advance(56)
    assert b.acquire(budget.COMMAND, limit="start").allowed


def test_a_start_refused_by_the_limiter_is_not_counted():
    c = Clock()
    b = make(c)
    for _ in range(budget.ACTION_LIMIT):
        b.acquire(budget.COMMAND, limit="start")
    for _ in range(3):
        assert b.acquire(budget.COMMAND, limit="start").reason == "limiter"
    c.advance(60)
    assert b.acquire(budget.COMMAND, limit="start").allowed


def test_snapshot_says_when_the_next_start_is_allowed():
    c = Clock()
    b = make(c)
    for _ in range(budget.ACTION_LIMIT):
        b.acquire(budget.COMMAND, limit="start")
        c.advance(2)
    assert b.snapshot.action_free_at == {"start": 1060.0}
    c.advance(60)
    b.check(budget.COMMAND)
    assert b.snapshot.action_free_at == {}


def test_an_unknown_limit_is_a_programming_error():
    b = make(Clock())
    with pytest.raises(ValueError):
        b.acquire(budget.COMMAND, limit="pause")


def test_an_unanswered_attempt_is_marked_until_anything_answers():
    c = Clock()
    b = make(c)
    b.acquire(budget.BACKGROUND)
    b.record(error=True, counts=False, network=True)
    assert b.snapshot.network_failed_mono == 1000.0
    c.advance(5)
    b.acquire(budget.BACKGROUND)
    b.record(error=True, counts=True)  # a 409 is still an answer
    assert b.snapshot.network_failed_mono == 0.0


def test_commands_leave_the_last_70_for_token_refresh_and_stream_reconnects():
    assert budget.COMMAND_RESERVE == 70
    c = Clock()
    b = make(c)
    spend(b, c, budget.BACKGROUND, 500)
    spend(b, c, budget.COMMAND, 380)
    assert b.snapshot.total_used == 880
    assert b.acquire(budget.COMMAND).reason == "ceiling"
    assert budget.COMMAND in b.snapshot.exhausted
    spend(b, c, budget.AUTH, 30)
    spend(b, c, budget.STREAM, 40)
    assert b.snapshot.total_used == budget.TOTAL
    assert b.acquire(budget.AUTH).reason == "total"


def test_the_snapshot_says_when_commands_are_allowed_again():
    c = Clock(wall=1_800_000_000.0)
    b = make(c)
    assert b.snapshot.command_free_at == 0.0
    spend(b, c, budget.BACKGROUND, 10, step=60.0)  # one a minute, the oldest first
    spend(b, c, budget.BACKGROUND, 490)
    spend(b, c, budget.COMMAND, 380)
    first_minute = 1_800_000_000 // 60
    # The oldest request must leave the window before the total drops under 880 again.
    free_at = b.snapshot.command_free_at
    assert free_at == (first_minute + budget.WINDOW_MINUTES) * 60
    c.advance(free_at - c.w - 1)
    assert b.acquire(budget.COMMAND).reason == "ceiling"
    c.advance(1)
    assert b.acquire(budget.COMMAND).allowed
