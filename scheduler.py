"""What to request next (spec 7.3, 7.4). Pure apart from a monotonic and a wall clock.

Every rule here exists to keep the daily budget for what the user sees: a runtime option value
arriving by NOTIFY never triggers a fetch, event-triggered refetches share one per-appliance cap,
and a key one refetch could not resolve never triggers another.
"""

import time
from collections import deque
from dataclasses import dataclass, field

import auth
import budget
import hc_client
import model as model_mod

PRIORITY_COMMAND = 0
PRIORITY_TOKEN = 1
PRIORITY_REFETCH = 2
PRIORITY_BACKGROUND = 3
CAP_PER_HOUR = 6
CAP_PER_DAY = 24
PROGRAM_SETTLE_S = 3.0
DAILY_REFRESH_S = 86400.0
UNKNOWN_APPLIANCE_LATCH_S = 3600.0
CAPPED_REPORT_S = 3600.0
EMPTY_PROGRAMS_RETRY_S = 3600.0
# Types whose program endpoints only ever answer 409 (cooling; the Home Connect
# programs-and-options page says "no programs available" for each) or that have no documented
# programs (hobs, spec 3.2a): fetching them would only spend budget and error responses.
# Cooktops are not listed because the docs do describe cooktop programs.
NO_PROGRAM_TYPES = frozenset({"FridgeFreezer", "Refrigerator", "Freezer", "WineCooler", "Hob"})


@dataclass(frozen=True)
class Job:
    kind: str
    method: str = "GET"
    path: str = ""
    ha_id: str = ""
    key: str = ""
    budget_class: str = budget.BACKGROUND
    priority: int = PRIORITY_BACKGROUND
    dedupe_key: str = ""
    state_stamp: str | None = None
    issued_seq: int = 0
    not_before: float = 0.0
    ttl_deadline: float = 0.0
    form: dict | None = field(default=None, repr=False)
    oauth: bool = False
    needs_token: bool = True
    auth_generation: int = 0
    retried: bool = False


def device_code_job(client_id: str) -> Job:
    return Job(
        "device_code",
        "POST",
        hc_client.DEVICE_AUTH_PATH,
        budget_class=budget.AUTH,
        priority=PRIORITY_TOKEN,
        form={"client_id": client_id, "scope": auth.SCOPE},
        oauth=True,
        needs_token=False,
    )


def token_poll_job(client_id: str, device_code: str, client_secret: str) -> Job:
    form = {"grant_type": "device_code", "device_code": device_code, "client_id": client_id}
    if client_secret:
        form["client_secret"] = client_secret  # docs: optional but recommended (spec 8.1)
    return Job(
        "token_poll",
        "POST",
        hc_client.TOKEN_PATH,
        budget_class=budget.AUTH,
        priority=PRIORITY_TOKEN,
        form=form,
        oauth=True,
        needs_token=False,
    )


def refresh_job(refresh_token: str, client_secret: str) -> Job:
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if client_secret:
        form["client_secret"] = client_secret
    return Job(
        "refresh",
        "POST",
        hc_client.TOKEN_PATH,
        # An OAuth request like the device flow: it passes a brake pause, which an expired
        # access token needs, and stops at the hard stop (budget._brake). The worker queue never
        # coalesces it; refresh is kept single-flight by the plugin (spec 4.3).
        budget_class=budget.AUTH,
        priority=PRIORITY_TOKEN,
        form=form,
        oauth=True,
        needs_token=False,
    )


def revoke_job(refresh_token: str, client_id: str, client_secret: str) -> Job:
    # RFC 7009 2.1: the revocation endpoint authenticates the client as the token endpoint does,
    # so the client identifies itself; without it the revoke is refused as an unknown client.
    form = {"token": refresh_token, "token_type_hint": "refresh_token", "client_id": client_id}
    if client_secret:
        form["client_secret"] = client_secret
    return Job(
        "revoke",
        "POST",
        hc_client.REVOKE_PATH,
        budget_class=budget.AUTH,
        priority=PRIORITY_TOKEN,
        form=form,
        oauth=True,
        needs_token=False,
    )


def _program_job(kind: str, ha_id: str, issued: int, **extra) -> Job:
    """`selected` or `active`: one stable dedupe key so resync, status follow-ups and event
    refetches for the same appliance coalesce in the worker queue."""
    return _api(kind, ha_id, "programs", kind, issued=issued, dedupe=f"{ha_id}:{kind}", **extra)


def _api(
    kind: str,
    ha_id: str,
    *tail: str,
    key: str = "",
    priority: int = PRIORITY_BACKGROUND,
    stamp: str | None = None,
    issued: int = 0,
    not_before: float = 0.0,
    dedupe: str = "",
) -> Job:
    path = hc_client.api_path("homeappliances", ha_id, *tail)
    return Job(
        kind,
        "GET",
        path,
        ha_id,
        key,
        budget.BACKGROUND,
        priority,
        dedupe,
        stamp,
        issued,
        not_before,
    )


class Scheduler:
    def __init__(self, mono=time.monotonic, wall=time.time):
        self._mono = mono
        self._wall = wall
        self._empty_retry: dict[str, float] = {}
        self._capped: list[str] = []
        self._capped_reported: dict[str, float] = {}
        self._refetches: dict[str, deque[float]] = {}
        self._unknown_seen: dict[str, float] = {}
        # Setting keys an event introduced, per appliance, mapped to the issued_seq of the
        # refetch that was queued for them. The event already put the key in the model, so only
        # the refetch's own answer can show whether the API knows it.
        self._awaiting: dict[str, dict[str, int]] = {}

    # -- caps -----------------------------------------------------------------------------------

    def _take_refetch(self, ha_id: str) -> bool:
        now = self._mono()
        history = self._refetches.setdefault(ha_id, deque())
        while history and now - history[0] >= 86400:
            history.popleft()
        last_hour = sum(1 for t in history if now - t < 3600)
        if last_hour >= CAP_PER_HOUR or len(history) >= CAP_PER_DAY:
            reported = self._capped_reported.get(ha_id)
            if reported is None or now - reported >= CAPPED_REPORT_S:
                self._capped_reported[ha_id] = now
                self._capped.append(ha_id)
            return False
        history.append(now)
        return True

    def take_capped(self) -> list[str]:
        """haIds whose refetch the cap refused since the last call, each at most once an hour."""
        capped, self._capped = self._capped, []
        return capped

    # -- builders -------------------------------------------------------------------------------

    def list_job(self, m: model_mod.Model) -> Job:
        return Job("list", "GET", "/api/homeappliances", issued_seq=m.tick(), dedupe_key="list")

    def _base(self, m: model_mod.Model, app: model_mod.Appliance, priority: int) -> list[Job]:
        """status, selected, settings for one appliance.

        `selected` is sent here, between two fetches that succeed, only when the program list
        is cached; without one, the `available` follow-up fetches it. Every appliance's base
        lands in the queue at once, and an idle appliance answers `selected` with 404
        NoProgramSelected, an error response for the brake: queued from each status answer
        instead, they would run back to back (spec 7.3 step 5).
        """
        jobs = [_api("status", app.ha_id, "status", priority=priority, issued=m.tick())]
        if self._program_type(app) and app.cache.programs:
            jobs.append(_program_job("selected", app.ha_id, m.tick(), priority=priority))
        jobs.append(_api("settings", app.ha_id, "settings", priority=priority, issued=m.tick()))
        return jobs

    def discovery(self, m: model_mod.Model, ha_ids) -> list[Job]:
        jobs: list[Job] = []
        for ha_id in ha_ids:
            app = m.appliances.get(ha_id)
            if app is not None and app.connected:
                app.resync_due = False
                jobs.extend(self._base(m, app, PRIORITY_BACKGROUND))
        return jobs

    def _setting_details(self, m: model_mod.Model, app: model_mod.Appliance) -> list[Job]:
        """Constraints for each setting key not cached yet, whatever AllowControl says: the
        device type of a setting is fixed when it is created (spec 7.3 step 6). The dedupe key
        coalesces the fetches two settings answers would otherwise both queue."""
        return [
            _api(
                "setting_detail",
                app.ha_id,
                "settings",
                key,
                key=key,
                issued=m.tick(),
                dedupe=f"{app.ha_id}:setting_detail:{key}",
            )
            for key in app.settings
            if key not in app.cache.setting_constraints and key not in app.cache.negative_keys
        ]

    def _resync(self, m: model_mod.Model, app: model_mod.Appliance) -> list[Job]:
        """One capped refetch for an appliance back from an offline API answer (model
        `resync_due`): what that answer refused is still missing, and no CONNECTED event comes
        to refetch it."""
        if not app.resync_due:
            return []
        app.resync_due = False
        return self._base(m, app, PRIORITY_REFETCH) if self._take_refetch(app.ha_id) else []

    def _program_type(self, app: model_mod.Appliance) -> bool:
        return app.type not in NO_PROGRAM_TYPES

    def _judge_awaited(self, app: model_mod.Appliance, job: Job) -> None:
        """Negative-cache the keys this settings answer did not resolve (spec 7.4).

        An answer that carried the key observed it after the request was issued, which moves its
        sequence number past `issued_seq`; a key whose sequence number did not move is absent from
        the answer.
        """
        awaiting = self._awaiting.get(app.ha_id)
        if not awaiting:
            return
        for key, queued_seq in list(awaiting.items()):
            if queued_seq > job.issued_seq:
                continue  # queued for a later refetch than this answer
            del awaiting[key]
            if app.seq.get(key, 0) <= job.issued_seq and key not in app.cache.negative_keys:
                app.cache.negative_keys.append(key)

    def after_response(self, m: model_mod.Model, job: Job, status: int) -> list[Job]:
        app = m.appliances.get(job.ha_id)
        if app is None:
            return []
        if status != 200:
            if job.kind in ("available", "commands"):
                # daily() is the only retry for these fetches, so a failed one must not keep the
                # stamp that says the caches were filled. It is retried an hour out, not at once:
                # a persistent 409 must not cost errors on every heartbeat and trip the brake.
                app.cache.refreshed_at = self._wall() - DAILY_REFRESH_S + EMPTY_PROGRAMS_RETRY_S
            return []
        jobs = self._resync(m, app)
        if job.kind == "status":
            # Queued here, so stamp now: a daily() call during a slow first install must not
            # emit the same fetches again. The answer-time stamps below record when data landed.
            if self._program_type(app):
                state = app.operation_state()
                if state is not None and state not in model_mod.IDLE_STATES:
                    jobs.append(_program_job("active", app.ha_id, m.tick()))
                elif not app.cache.programs:
                    app.cache.refreshed_at = self._wall()
                    self._empty_retry[app.ha_id] = self._wall()
                    jobs.append(
                        _api(
                            "available",
                            app.ha_id,
                            "programs",
                            "available",
                            stamp=state,
                            issued=m.tick(),
                        )
                    )
            if not app.cache.commands:
                app.cache.refreshed_at = self._wall()
                jobs.append(_api("commands", app.ha_id, "commands", issued=m.tick()))
        elif job.kind == "available":
            app.cache.refreshed_at = self._wall()
            # selected may answer 404 (nothing selected): it follows a success and precedes the
            # detail fetches, so expected errors never run back to back (spec 7.3).
            jobs.append(_program_job("selected", app.ha_id, m.tick()))
            for key in app.cache.programs:
                if key not in app.cache.program_details:
                    jobs.append(
                        _api(
                            "program_detail",
                            app.ha_id,
                            "programs",
                            "available",
                            key,
                            key=key,
                            issued=m.tick(),
                        )
                    )
        elif job.kind == "commands":
            app.cache.refreshed_at = self._wall()
        elif job.kind == "settings":
            self._judge_awaited(app, job)
            jobs += self._setting_details(m, app)
        return jobs

    def on_effect(self, m: model_mod.Model, effect: model_mod.EventEffect) -> list[Job]:
        if effect.ha_id is None:
            return []
        if effect.unknown_appliance:
            last = self._unknown_seen.get(effect.ha_id)
            if last is not None and self._mono() - last < UNKNOWN_APPLIANCE_LATCH_S:
                return []
            self._unknown_seen[effect.ha_id] = self._mono()
            return [self.list_job(m)]
        app = m.appliances.get(effect.ha_id)
        if app is None:
            return []
        if effect.kind in ("connected", "paired"):
            if self._take_refetch(app.ha_id):
                return self._base(m, app, PRIORITY_REFETCH)
            return []
        jobs = self._resync(m, app)
        if effect.program_changed:
            kind = effect.program_changed
            jobs.append(
                _api(
                    kind,
                    app.ha_id,
                    "programs",
                    kind,
                    priority=PRIORITY_REFETCH,
                    issued=m.tick(),
                    not_before=self._mono() + PROGRAM_SETTLE_S,
                    dedupe=f"{app.ha_id}:{kind}",
                )
            )
        fresh = [k for k in effect.unknown_settings if k not in app.cache.negative_keys]
        if fresh and self._take_refetch(app.ha_id):
            job = _api(
                "settings",
                app.ha_id,
                "settings",
                key=fresh[0],
                priority=PRIORITY_REFETCH,
                issued=m.tick(),
                dedupe=f"{app.ha_id}:settings",
            )
            awaiting = self._awaiting.setdefault(app.ha_id, {})
            for key in fresh:
                awaiting[key] = job.issued_seq
            jobs.append(job)
        return jobs

    def on_stream_reconnected(self, m: model_mod.Model) -> list[Job]:
        jobs: list[Job] = []
        for app in m.appliances.values():
            if not app.connected or not self._take_refetch(app.ha_id):
                continue
            jobs.append(
                _api("status", app.ha_id, "status", priority=PRIORITY_REFETCH, issued=m.tick())
            )
            if not self._program_type(app):
                continue
            jobs.append(_program_job("selected", app.ha_id, m.tick(), priority=PRIORITY_REFETCH))
            if not app.is_idle():
                jobs.append(_program_job("active", app.ha_id, m.tick(), priority=PRIORITY_REFETCH))
        return jobs

    def daily(self, m: model_mod.Model, now_wall: float) -> list[Job]:
        for app in m.appliances.values():
            if not app.connected or not app.is_idle():
                continue
            stale = now_wall - app.cache.refreshed_at >= DAILY_REFRESH_S
            # A program appliance that was running at first discovery (or whose `available` failed)
            # has no program list and nothing else retries it, so it is due whatever the stamp
            # says. The spacing keeps an appliance that really answers an empty list from being
            # asked on every heartbeat; the jobs' dedupe keys and the error brake bound the rest.
            unfilled = (
                self._program_type(app)
                and not app.cache.programs
                and now_wall - self._empty_retry.get(app.ha_id, -EMPTY_PROGRAMS_RETRY_S)
                >= EMPTY_PROGRAMS_RETRY_S
            )
            if not (stale or unfilled):
                continue
            app.cache.refreshed_at = now_wall
            if unfilled:
                self._empty_retry[app.ha_id] = now_wall
            jobs = [_api("commands", app.ha_id, "commands", issued=m.tick())]
            if self._program_type(app):
                jobs.insert(
                    0,
                    _api(
                        "available",
                        app.ha_id,
                        "programs",
                        "available",
                        stamp=app.operation_state(),
                        issued=m.tick(),
                    ),
                )
            jobs += self._setting_details(m, app)
            return jobs  # one appliance per call spreads the refresh over heartbeats
        return []
