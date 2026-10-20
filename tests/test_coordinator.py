import pytest

import catalog
import control
import coordinator
import model

HA = "SIEMENS-HCS01OVN1-000000000001"
HA2 = "BOSCH-HCS02DWH1-000000000002"
SETPOINT = "Cooking.Oven.Option.SetpointTemperature"
STATE = "BSH.Common.EnumType.OperationState."


def make():
    return coordinator.Coordinator("en", lambda ha: "Oven")


def write(action="option", key=SETPOINT, value=200, ha=HA, **kw):
    request = control.Request("PUT", f"/api/homeappliances/{ha}/x")
    base = {"coalesce": f"option:{key}", "verify": "selected"} if action == "option" else {}
    base.update(kw)
    return control.Intent(ha, action, key, value, request, f"{action} {value}", **base)


def start(ha=HA):
    return write("start", "P", None, ha, limit="start", verify="status")


def answered(c, w, status=204, category="", key="", description="", now=1.0):
    return c.on_answer(w.id, status, category, key, description, now)


def seen(store_name, key, value, seq):
    app = model.Appliance(HA)
    getattr(app, store_name)[key] = model.Value(value)
    app.seq[key] = seq
    return app


def test_one_write_per_appliance_in_flight_and_appliances_take_turns():
    c = make()
    c.submit(write(value=1), 0.0)
    c.submit(write("setting", "K.Setting.A", True, coalesce="setting:K.Setting.A"), 0.0)
    c.submit(write(ha=HA2, value=2), 0.0)
    sent, _ = c.dispatch(0.0)
    assert sorted(w.intent.ha_id for w in sent) == sorted([HA, HA2])
    assert c.dispatch(0.0) == ([], [])
    first = next(w for w in sent if w.intent.ha_id == HA)
    answered(c, first)
    following, _ = c.dispatch(1.0)
    assert [w.intent.key for w in following] == ["K.Setting.A"]


def test_rapid_setpoint_clicks_send_one_write_with_the_last_value():
    c = make()
    for i, value in enumerate((185, 190, 195, 200)):
        c.submit(write(value=value, debounce=True), i * 0.5)
    settled = 1.5 + coordinator.SETPOINT_DEBOUNCE_S
    assert c.dispatch(settled - 0.1)[0] == []
    sent, _ = c.dispatch(settled)
    assert [w.intent.value for w in sent] == [200]
    answered(c, sent[0], now=settled)
    assert c.dispatch(10.0)[0] == []


def test_stop_cancels_a_start_not_yet_sent():
    c = make()
    c.submit(write(value=200, debounce=True), 0.0)
    c.submit(start(), 0.0)
    notes = c.submit(control.Intent(HA, "stop", "", None, None, "Stop"), 1.0)
    assert [n.event for n in notes] == ["command_cancelled"]
    assert notes[0].fields["reason"] == "Cancelled by Stop"
    assert c.pending(HA) == (None, False, False)
    sent, _ = c.dispatch(5.0)
    assert [w.intent.action for w in sent] == ["option"]
    c.sent(sent[0], 5)
    answered(c, sent[0], now=5.5)
    c.tick({HA: seen("options", SETPOINT, 200, 6)}, 6.0)
    assert c.dispatch(6.0)[0] == [] and not c.busy()


def test_a_stop_that_has_something_to_stop_is_queued():
    c = make()
    c.submit(write("stop", "", None, limit="stop", verify="status"), 0.0)
    sent, _ = c.dispatch(0.0)
    assert [w.intent.action for w in sent] == ["stop"]


def test_a_failed_write_blocks_a_start_behind_it_for_a_minute():
    c = make()
    c.submit(write(value=200), 0.0)
    c.submit(start(), 0.0)
    [w], _ = c.dispatch(0.0)
    notes = answered(c, w, 409, "refused_validation", "SDK.Error.InvalidOptionValue", "Too hot")
    assert (notes[0].event, notes[0].fields["reason"]) == ("command_failed", "Too hot")
    sent, notes = c.dispatch(1.0)
    assert sent == []
    assert notes[0].fields["reason"] == "Start not sent: option 200 failed"
    c.submit(start(), 62.0)
    assert [x.intent.action for x in c.dispatch(62.0)[0]] == ["start"]


def test_a_start_waits_for_earlier_writes_to_be_confirmed_and_asks_for_them():
    c = make()
    c.submit(write(value=200), 0.0)
    c.submit(start(), 0.0)
    [w], _ = c.dispatch(0.0)
    c.sent(w, 5)
    answered(c, w)
    assert c.dispatch(1.0)[0] == []
    _, verify = c.tick({}, 1.0)
    assert [v.id for v in verify] == [w.id]  # asked for at once, not at the deadline
    c.on_verified(w.id, seen("options", SETPOINT, 200, 6), 2.0)
    assert [x.intent.action for x in c.dispatch(2.0)[0]] == ["start"]


def test_confirmation_by_an_event_ends_the_overlay():
    c = make()
    c.submit(write(value=200, overlay=(20, 0, "200.0")), 0.0)
    assert c.overlay() == {(HA, 20): (0, "200.0")}
    [w], _ = c.dispatch(0.0)
    c.sent(w, 5)
    answered(c, w)
    assert c.overlay() == {(HA, 20): (0, "200.0")}
    notes, verify = c.tick({HA: seen("options", SETPOINT, 200.0, 6)}, 2.0)
    assert [n.event for n in notes] == ["command_confirmed"] and verify == []
    assert c.overlay() == {} and not c.busy()


def test_an_unconfirmed_write_is_checked_once_at_the_deadline_then_reported():
    c = make()
    c.submit(write(value=200, overlay=(20, 0, "200.0")), 0.0)
    [w], _ = c.dispatch(0.0)
    c.sent(w, 5)
    answered(c, w)
    assert c.tick({}, 30.0)[1] == []
    assert c.tick({}, 1.0 + coordinator.CONFIRM_S)[1] == [w]
    assert c.tick({}, 2.0 + coordinator.CONFIRM_S)[1] == []
    notes = c.on_verified(w.id, seen("options", SETPOINT, 180, 7), 32.0)
    assert notes[0].event == "command_unconfirmed"
    assert c.overlay() == {}


def test_a_lost_answer_is_reconciled_once_and_never_resent():
    c = make()
    c.submit(start(), 0.0)
    [w], _ = c.dispatch(0.0)
    c.sent(w, 5)
    notes = answered(c, w, 0, "network")
    assert notes[0].event == "command_no_answer"
    assert c.tick({}, 1.0)[1] == [w]
    assert c.dispatch(2.0)[0] == [] and c.tick({}, 2.0)[1] == []
    app = seen("status", model.OPERATION_STATE, STATE + "Ready", 6)
    notes = c.on_verified(w.id, app, 3.0)
    assert notes[0].event == "command_unconfirmed"
    assert "did not start" in notes[0].fields["reason"]
    assert c.dispatch(4.0)[0] == [] and not c.busy()


def test_a_lost_write_that_the_check_confirms_counts_as_done():
    c = make()
    c.submit(write(value=200), 0.0)
    [w], _ = c.dispatch(0.0)
    c.sent(w, 5)
    answered(c, w, 503, "server")
    c.tick({}, 1.0)
    assert c.on_verified(w.id, seen("options", SETPOINT, 200, 6), 2.0)[0].event == (
        "command_confirmed"
    )


def test_a_write_without_a_way_to_confirm_ends_with_its_answer():
    c = make()
    c.submit(write("command", catalog.OPEN_DOOR, True), 0.0)
    [w], _ = c.dispatch(0.0)
    assert answered(c, w)[0].event == "command_confirmed"
    c.submit(write("command", catalog.ACKNOWLEDGE, True), 2.0)
    [w], _ = c.dispatch(2.0)
    notes = answered(c, w, 0, "network", now=3.0)
    assert notes[0].event == "command_unconfirmed" and c.tick({}, 3.0)[1] == []


def test_queued_writes_expire_and_unanswered_ones_are_settled():
    c = make()
    c.submit(write(value=1), 0.0)
    [w], _ = c.dispatch(0.0)
    c.submit(write("setting", "K.Setting.B", True, coalesce="setting:K.Setting.B"), 0.0)
    notes, _ = c.tick({}, 30.5)
    assert [n.event for n in notes] == ["command_expired"]
    notes, verify = c.tick({}, coordinator.IN_FLIGHT_MAX_S + 0.5)
    assert [n.event for n in notes] == ["command_no_answer"] and verify == [w]


def test_worker_refusals_end_the_write_with_a_reason():
    c = make()
    c.submit(start(), 0.0)
    [w], _ = c.dispatch(0.0)
    notes = c.on_refused(w.id, "limiter", None, 1.0)
    assert notes[0].event == "command_failed"
    assert notes[0].fields["reason"] == control.refused_reason("limiter", "en")
    c.submit(start(), 2.0)  # a refused Start is no prerequisite of the next one
    [w], _ = c.dispatch(2.0)
    assert c.on_refused(w.id, "expired", None, 3.0)[0].event == "command_expired"


def test_a_refused_check_reports_the_write_unconfirmed():
    c = make()
    c.submit(write(value=200), 0.0)
    [w], _ = c.dispatch(0.0)
    answered(c, w, 0, "network")
    c.tick({}, 1.0)
    assert c.on_refused(w.id, "brake", None, 2.0)[0].event == "command_unconfirmed"


def test_a_write_that_cannot_be_handed_over_fails():
    c = make()
    c.submit(write(value=1), 0.0)
    [w], _ = c.dispatch(0.0)
    notes = c.unsent(w, 0.5)
    assert notes[0].fields["reason"] == control.refused_reason("queue_full", "en")
    assert c.unsent(w, 0.5, "rebuilt and refused")[0].fields["reason"] == "rebuilt and refused"


def test_lost_authorization_cancels_what_waits_and_never_calls_a_sent_write_failed():
    c = make()
    c.submit(write(value=1), 0.0)
    [w], _ = c.dispatch(0.0)
    c.submit(start(), 0.0)
    notes = c.abandon(1.0)
    assert sorted(n.event for n in notes) == ["command_cancelled", "command_unconfirmed"]
    unconfirmed = next(n for n in notes if n.event == "command_unconfirmed")
    assert unconfirmed.fields["reason"] == control.message("unconfirmed", "en", a="Oven")
    assert answered(c, w, now=2.0) == [] and not c.busy()


def test_pending_reports_the_selection_and_a_waiting_start():
    c = make()
    select = write("select", "P2", "P2", coalesce="select", verify="selected", confirm_by_get=True)
    c.submit(select, 0.0)
    c.submit(start(), 0.0)
    assert c.pending(HA) == ("P2", True, False) and c.pending(HA2) == (None, False, False)
    assert c.busy()


def test_a_selection_is_confirmed_by_its_get_not_by_an_event():
    c = make()
    select = write("select", "P2", "P2", coalesce="select", verify="selected", confirm_by_get=True)
    c.submit(select, 0.0)
    [w], _ = c.dispatch(0.0)
    c.sent(w, 5)
    answered(c, w)
    app = model.Appliance(HA, selected_program="P2")
    app.seq[model.SELECTED_ROOT] = 6
    notes, verify = c.tick({HA: app}, 1.0)
    assert notes == [] and verify == [w]
    assert c.on_verified(w.id, app, 2.0)[0].event == "command_confirmed"


DELAY_KEY = "BSH.Common.Option.StartInRelative"


def delay_move(seconds=3600):
    return write("delay", DELAY_KEY, seconds, coalesce=f"option:{DELAY_KEY}")


def test_the_overlay_stays_until_the_check_decides_a_lost_answer():
    c = make()
    c.submit(write(value=200, overlay=(20, 0, "200.0")), 0.0)
    [w], _ = c.dispatch(0.0)
    c.sent(w, 5)
    answered(c, w, 0, "network")
    assert c.overlay() == {(HA, 20): (0, "200.0")}
    assert c.tick({}, 1.0)[1] == [w]
    assert c.overlay() == {(HA, 20): (0, "200.0")}
    assert c.on_verified(w.id, seen("options", SETPOINT, 200, 6), 2.0)[0].event == (
        "command_confirmed"
    )
    assert c.overlay() == {}


def test_a_start_sent_but_not_settled_is_reported_apart_from_a_waiting_one():
    c = make()
    c.submit(start(), 0.0)
    [w], _ = c.dispatch(0.0)
    c.submit(start(), 0.5)
    assert c.pending(HA) == (None, True, True)
    c.submit(control.Intent(HA, "stop", "", None, None, "Stop"), 0.6)
    assert c.pending(HA) == (None, False, True)
    c.sent(w, 5)
    answered(c, w, 0, "network")
    assert c.pending(HA) == (None, False, True)
    c.tick({}, 1.0)
    assert c.pending(HA) == (None, False, True)
    c.on_verified(w.id, seen("status", model.OPERATION_STATE, STATE + "Ready", 6), 2.0)
    assert c.pending(HA) == (None, False, False)


def follow_stop():
    request = control.stop_request(HA)
    return control.Intent(
        HA, "stop", "", None, request, "Stop", limit="stop", verify="status", follows_start=True
    )


def started():
    c = make()
    c.submit(start(), 0.0)
    [w], _ = c.dispatch(0.0)
    c.sent(w, 5)
    c.submit(follow_stop(), 0.5)
    assert c.dispatch(0.5) == ([], [])
    return c, w


def nothing_to_stop(notes):
    reason = control.message("nothing_to_stop", "en", a="Oven")
    return [(n.event, n.fields.get("reason")) for n in notes] == [("command_cancelled", reason)]


def test_a_stop_that_follows_an_accepted_start_is_sent():
    c, w = started()
    answered(c, w)
    [stop], _ = c.dispatch(1.0)
    assert stop.intent.request == control.stop_request(HA)


def test_a_stop_that_follows_a_confirmed_start_is_sent():
    c, w = started()
    answered(c, w, 0, "network")
    c.tick({}, 1.0)
    c.on_verified(w.id, seen("status", model.OPERATION_STATE, STATE + "Run", 6), 2.0)
    assert [x.intent.action for x in c.dispatch(2.0)[0]] == ["stop"]


def test_a_stop_waits_while_its_start_is_being_checked():
    c, w = started()
    answered(c, w, 0, "network")
    assert c.dispatch(1.0) == ([], [])
    c.tick({}, 1.0)
    assert c.dispatch(1.5) == ([], [])
    c.on_verified(w.id, seen("status", model.OPERATION_STATE, STATE + "Ready", 6), 2.0)
    sent, notes = c.dispatch(2.0)
    assert sent == [] and nothing_to_stop(notes)
    assert not c.busy()


def _start_failed(c, w):
    answered(c, w, 409, "refused_validation", "", "No")


def _start_refused(c, w):
    c.on_refused(w.id, "limiter", None, 1.0)


def _start_expired(c, w):
    c.on_refused(w.id, "expired", None, 1.0)


@pytest.mark.parametrize("outcome", [_start_failed, _start_refused, _start_expired])
def test_a_stop_that_follows_a_start_that_did_not_go_is_cancelled(outcome):
    c, w = started()
    outcome(c, w)
    sent, notes = c.dispatch(1.0)
    assert sent == [] and nothing_to_stop(notes)


def test_a_stop_following_a_start_and_given_while_none_is_open_is_a_plain_stop():
    c = make()
    c.submit(follow_stop(), 0.0)
    assert [x.intent.action for x in c.dispatch(0.0)[0]] == ["stop"]


def test_a_value_given_after_a_start_does_not_replace_one_the_start_relies_on():
    c = make()
    c.submit(write(value=190, debounce=True), 0.0)
    c.submit(start(), 0.5)
    notes = c.submit(write(value=200, debounce=True), 1.0)
    assert [n.event for n in notes] == ["command_queued"]
    [first], _ = c.dispatch(2.0)
    assert first.intent.value == 190
    c.sent(first, 5)
    answered(c, first, now=2.5)
    c.tick({HA: seen("options", SETPOINT, 190, 6)}, 2.6)
    [second], _ = c.dispatch(3.0)
    assert second.intent.action == "start"
    answered(c, second, now=3.5)
    [third], _ = c.dispatch(4.0)
    assert third.intent.value == 200


def test_values_given_after_a_start_still_replace_each_other():
    c = make()
    c.submit(start(), 0.0)
    c.submit(write("setting", "K.Setting.A", True, coalesce="setting:K.Setting.A"), 0.0)
    notes = c.submit(write("setting", "K.Setting.A", False, coalesce="setting:K.Setting.A"), 0.5)
    assert [n.event for n in notes] == ["command_replaced", "command_queued"]


@pytest.mark.parametrize("earlier", ["select", "option", "setting", "delay"])
def test_a_start_waits_for_every_earlier_prerequisite_and_never_follows_a_failed_one(earlier):
    c = make()
    c.submit(write(earlier, "K.Option.X", 1, coalesce=f"{earlier}:x"), 0.0)
    c.submit(start(), 0.0)
    [w], _ = c.dispatch(0.0)
    assert c.dispatch(0.5) == ([], [])
    answered(c, w, 409, "refused_validation", "", "No", now=1.0)
    sent, notes = c.dispatch(1.0)
    assert sent == [] and [n.event for n in notes] == ["command_failed"]


def test_the_start_handed_out_is_the_queued_write_for_the_caller_to_rebuild():
    c = make()
    c.submit(write("setting", "K.Setting.A", True), 0.0)
    queued = start()
    c.submit(queued, 0.0)
    [w], _ = c.dispatch(0.0)
    answered(c, w)
    [s], _ = c.dispatch(1.0)
    assert s.intent is queued and s.created == 0.0
    assert c.unsent(s, 1.0, "Oven is not ready")[0].fields["reason"] == "Oven is not ready"
    assert not c.busy()


def test_a_moved_delayed_start_is_held_once_its_write_is_confirmed():
    c = make()
    notes = c.submit(delay_move(), 0.0, hold=17 * 60)
    assert all(n.hold is None for n in notes)
    [w], _ = c.dispatch(0.0)
    notes = answered(c, w)
    assert [(n.event, n.hold) for n in notes] == [("command_confirmed", 17 * 60)]


def _refused(c, w):
    return c.on_refused(w.id, "limiter", None, 1.0)


def _failed(c, w):
    return answered(c, w, 409, "refused_validation", "", "No")


def _lost(c, w):
    return answered(c, w, 0, "network")


def _unsent(c, w):
    return c.unsent(w, 1.0)


@pytest.mark.parametrize("outcome", [_refused, _failed, _lost, _unsent])
def test_a_moved_delayed_start_that_did_not_take_keeps_the_old_time(outcome):
    c = make()
    c.submit(delay_move(), 0.0, hold=17 * 60)
    [w], _ = c.dispatch(0.0)
    notes = outcome(c, w)
    assert notes and all(n.hold is None for n in notes)
    assert all(n.hold is None for n in c.tick({}, 100.0)[0])


def test_a_replaced_delayed_start_move_holds_only_the_last_time():
    c = make()
    c.submit(delay_move(), 0.0)
    [first], _ = c.dispatch(0.0)
    c.submit(delay_move(3000), 0.5, hold=16 * 60)
    c.submit(delay_move(4000), 1.0, hold=18 * 60)
    assert [n.hold for n in answered(c, first, now=1.5)] == [None]
    [last], _ = c.dispatch(2.0)
    assert last.intent.value == 4000
    assert [n.hold for n in answered(c, last, now=2.5)] == [18 * 60]


def test_an_answer_for_an_unknown_or_settled_write_is_ignored():
    c = make()
    assert c.on_answer(99, 204, "", "", "", 0.0) == []
    assert c.on_verified(99, None, 0.0) == []
    assert c.on_refused(99, "limiter", None, 0.0) == []
    c.submit(write("command", catalog.OPEN_DOOR, True), 0.0)
    [w], _ = c.dispatch(0.0)
    answered(c, w)
    assert answered(c, w) == [] and c.on_refused(w.id, "limiter", None, 1.0) == []
    assert c.on_verified(w.id, None, 1.0) == []


def test_settled_writes_are_forgotten_after_the_prerequisite_window():
    c = make()
    c.submit(write(value=1), 0.0)
    [w], _ = c.dispatch(0.0)
    answered(c, w, 409, "refused_validation", "", "No")
    c.tick({}, 1.0 + coordinator.PREREQUISITE_WINDOW_S + 0.1)
    assert c.on_answer(w.id, 204, "", "", "", 70.0) == [] and c.overlay() == {}
    assert c.pending(HA) == (None, False, False)


def test_a_queued_write_past_its_ttl_expires_at_dispatch():
    c = make()
    c.submit(write(value=1), 0.0)
    sent, notes = c.dispatch(control.COMMAND_TTL_S + 0.5)
    assert sent == [] and [n.event for n in notes] == ["command_expired"]


def test_abandon_names_the_reason_it_is_given():
    c = make()
    c.submit(start(), 0.0)
    (note,) = c.abandon(1.0, "stopping")
    assert note.event == "command_cancelled"
    assert note.fields["reason"] == control.message("stopping", "en")


@pytest.mark.parametrize("lost_answer", [False, True])
@pytest.mark.parametrize("held, event", [(200, "command_confirmed"), (180, "command_unconfirmed")])
def test_a_check_never_answered_settles_on_the_model(lost_answer, held, event):
    # The GET can be lost without a word: dropped while a token refresh was due, or with the
    # queue of a worker that died. The write must still settle, or its overlay and the fast
    # heartbeat stay and every later Start on the appliance waits behind it until it expires.
    c = make()
    c.submit(write(value=200, overlay=(20, 0, "200.0")), 0.0)
    [w], _ = c.dispatch(0.0)
    c.sent(w, 5)
    answered(c, w, 0, "network") if lost_answer else answered(c, w)
    checked_at = 1.0 if lost_answer else 1.0 + coordinator.CONFIRM_S
    assert c.tick({}, checked_at)[1] == [w]
    late = checked_at + coordinator.IN_FLIGHT_MAX_S
    assert c.tick({}, late)[0] == [] and c.busy()
    notes, verify = c.tick({HA: seen("options", SETPOINT, held, 6)}, late + 0.5)
    assert [n.event for n in notes] == [event] and verify == []
    assert c.overlay() == {} and not c.busy()
    # A later Start is decided at once: sent after a confirmed value, refused after one that
    # did not take, never left waiting.
    c.submit(start(), late + 1.0)
    sent, notes = c.dispatch(late + 1.0)
    if held == 200:
        assert [x.intent.action for x in sent] == ["start"]
    else:
        assert sent == [] and [n.event for n in notes] == ["command_failed"]


def test_abandon_settles_accepted_and_checked_writes_as_unconfirmed():
    c = make()
    c.submit(write(value=200, overlay=(20, 0, "200.0")), 0.0)
    c.submit(write(ha=HA2, value=1), 0.0)
    sent, _ = c.dispatch(0.0)
    for w in sent:
        c.sent(w, 5)
    accepted = next(w for w in sent if w.intent.ha_id == HA)
    checked = next(w for w in sent if w.intent.ha_id == HA2)
    answered(c, accepted)
    answered(c, checked, 0, "network")
    c.tick({}, 1.0)
    notes = c.abandon(2.0)
    assert [n.event for n in notes] == ["command_unconfirmed", "command_unconfirmed"]
    assert c.overlay() == {} and not c.busy()
    assert c.on_verified(checked.id, seen("options", SETPOINT, 1, 9), 3.0) == []


def test_abandon_cancels_a_sent_write_known_not_to_have_run():
    # A write answered 401 was not executed and waits for a refreshed token; when the refresh
    # fails it was never sent, so it is cancelled, not unconfirmed.
    c = make()
    c.submit(write(value=1), 0.0)
    c.submit(write(ha=HA2, value=2), 0.0)
    sent, _ = c.dispatch(0.0)
    held = next(w for w in sent if w.intent.ha_id == HA)
    notes = c.abandon(1.0, unsent=[control.write_job(held.id, held.intent, 1, 30.0), None])
    events = {n.ha_id: (n.event, n.fields["reason"]) for n in notes}
    assert events[HA] == ("command_cancelled", control.message("not_authorized", "en"))
    assert events[HA2][0] == "command_unconfirmed"


def test_pending_keys_name_the_values_with_a_write_not_settled():
    c = make()
    c.submit(write(value=200), 0.0)
    c.submit(write("setting", "K.Setting.A", True, coalesce="setting:K.Setting.A"), 0.0)
    c.submit(start(), 0.0)
    assert c.pending_keys(HA) == frozenset({SETPOINT, "K.Setting.A"})
    assert c.pending_keys(HA2) == frozenset()
    [w], _ = c.dispatch(0.0)
    answered(c, w, 409, "refused_validation", "SDK.Error.InvalidOptionValue")
    assert c.pending_keys(HA) == frozenset({"K.Setting.A"})


def test_a_stop_that_expires_behind_an_unconfirmed_start_says_to_choose_stop_again():
    c = make()
    c.submit(start(), 0.0)
    [w], _ = c.dispatch(0.0)
    stop = control.Intent(
        HA, "stop", "", None, control.stop_request(HA), "Stop", limit="stop",
        verify="status", follows_start=True,
    )  # fmt: skip
    c.submit(stop, 1.0)
    notes, _ = c.tick({}, 1.0 + control.COMMAND_TTL_S + 0.5)
    assert [n.event for n in notes] == ["command_expired"]
    assert notes[0].fields["reason"].endswith("; choose Stop again")


def test_a_state_refusal_of_a_write_is_explained_from_the_model():
    c = make()
    c.submit(write("stop", "", None, limit="stop", verify="status"), 0.0)
    [w], _ = c.dispatch(0.0)
    app = seen("status", model.OPERATION_STATE, STATE + "Ready", 3)
    notes = c.on_answer(w.id, 404, "state", "SDK.Error.NoProgramActive", "", 1.0, app)
    assert notes[0].fields["reason"] == control.message("nothing_to_stop", "en", a="Oven")


def test_a_worker_refusal_names_its_wait():
    c = make()
    c.submit(start(), 0.0)
    [w], _ = c.dispatch(0.0)
    notes = c.on_refused(w.id, "limiter", None, 1.0, wait_s=20.0)
    assert notes[0].fields["reason"].endswith("try again in 20 s")


def test_a_queued_selection_with_writes_behind_it_is_not_replaced():
    # The writes behind it were judged against its program, so a later selection joins the end.
    c = make()
    c.submit(write(value=1), 0.0)
    c.dispatch(0.0)
    select = {"coalesce": "select", "verify": "selected", "confirm_by_get": True}
    c.submit(write("select", "P1", "P1", **select), 0.0)
    c.submit(write("setting", "K.Setting.A", True, coalesce="setting:K.Setting.A"), 0.0)
    notes = c.submit(write("select", "P2", "P2", **select), 0.0)
    assert "command_replaced" not in [n.event for n in notes]
    assert c.pending(HA)[0] == "P2"
    keys = [w.intent.key for w in c._writes[HA] if w.state == coordinator.QUEUED]
    assert keys == ["P1", "K.Setting.A", "P2"]
    notes = c.submit(write("select", "P3", "P3", **select), 0.0)
    assert [n.event for n in notes] == ["command_replaced", "command_queued"]


def test_a_write_the_plugin_drops_unsent_fails_with_the_reason_and_nothing_else_changes():
    c = make()
    c.submit(write(value=1), 0.0)
    c.submit(write(ha=HA2, value=2), 0.0)
    sent, _ = c.dispatch(0.0)
    dropped = next(w for w in sent if w.intent.ha_id == HA)
    job = control.write_job(dropped.id, dropped.intent, 1, 30.0)
    check = control.verify_job(dropped.id, dropped.intent, 2, 30.0)
    notes = c.drop([job, check], 1.0, "stale")
    assert [(n.ha_id, n.event) for n in notes] == [(HA, "command_failed")]
    assert notes[0].fields["reason"] == control.message("stale", "en")
    assert c.drop([job], 2.0, "stale") == []  # settled already
    assert c.busy()  # the other appliance's write is still open


def test_each_command_outcome_is_logged_at_status_and_progress_at_debug():
    levels = {}

    def record(notes):
        for n in notes:
            levels[n.event] = n.level

    c = make()
    record(c.submit(write(value=200, overlay=(20, 0, "200.0")), 0.0))
    [w], _ = c.dispatch(0.0)
    c.sent(w, 5)
    record(answered(c, w))
    record(c.tick({HA: seen("options", SETPOINT, 200.0, 6)}, 2.0)[0])
    c.submit(start(), 3.0)
    [w], _ = c.dispatch(3.0)
    record(answered(c, w, 409, "refused_validation", "SDK.Error.InvalidOptionValue", "Bad"))
    c.submit(start(), 70.0)
    record(c.abandon(71.0, "stopping"))
    c.submit(write(value=1), 80.0)
    record(c.tick({}, 120.0)[0])
    c.submit(write(value=200, overlay=(20, 0, "200.0")), 130.0)
    [w], _ = c.dispatch(130.0)
    c.sent(w, 131)
    answered(c, w, now=131.0)
    record(c.abandon(132.0, "stopping"))
    assert levels == {
        "command_queued": "debug",
        "command_accepted": "debug",
        "command_confirmed": "status",
        "command_failed": "status",
        "command_cancelled": "status",
        "command_expired": "status",
        "command_unconfirmed": "status",
    }
