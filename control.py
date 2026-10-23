"""Domoticz commands to Home Connect writes (spec 7.6). Pure.

decide() turns one onCommand into a write for the coordinator, a delayed-start time the plugin
holds, or a refusal that names the fix. Its checks run on the model and a budget snapshot and
are advisory: the worker's budget check stays authoritative (spec 4.3), but a refusal found
here costs no request and no error response. Every message exists in English and Dutch (spec
5.1); Home Connect's own error descriptions are English only (spec 3.2).
"""

import math
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

import budget
import catalog
import errors
import hc_client
import model
import persistence
import planner
import scheduler
import text

COMMAND_TTL_S = 30.0
# Commands are refused for this long after a request got no HTTP answer at all (spec 7.1),
# unless something has answered since.
NETWORK_BACKOFF_S = 60.0
# A delayed-start time closer than this is taken as the same time tomorrow: the program could
# not start "at" it anyway.
DELAY_MIN_LEAD_S = 60.0
RUNNING_STATES = frozenset({"Run", "Pause", "DelayedStart", "ActionRequired"})
# The not_ready message per state whose fix differs from waiting until the appliance is Ready.
_NOT_READY = {
    "Inactive": "not_ready_off",
    "Finished": "not_ready_finished",
    "DelayedStart": "not_ready_waiting",
}
# The state a program command needs, and the state that confirms it (spec 3.2a).
COMMAND_STATES = {catalog.PAUSE: ("Run", "Pause"), catalog.RESUME: ("Pause", "Run")}
_REASON_MAX = 150
_VERIFY_PATHS = {
    "status": ("status",),
    "settings": ("settings",),
    "selected": ("programs", "selected"),
    "active": ("programs", "active"),
}

# (English, Dutch). Short: each lands in one log line.
_TEXT = {
    "control_off": (
        "'Allow control' is off in the hardware settings; turn it on under Setup > Hardware to"
        " operate appliances",
        "'Allow control' staat uit in de hardware-instellingen; zet het aan onder Instellingen >"
        " Hardware om apparaten te bedienen",
    ),
    "read_only": (
        "This device only shows a value; it cannot be changed from Domoticz",
        "Dit apparaat toont alleen een waarde; het kan niet vanuit Domoticz worden gewijzigd",
    ),
    "unmapped": (
        "Selector levels unknown; restore a backup or delete the device",
        "Selectorniveaus onbekend; zet een back-up terug of verwijder het apparaat",
    ),
    "unknown_appliance": (
        "Appliance not discovered yet; try again shortly",
        "Apparaat nog niet gevonden; probeer het zo opnieuw",
    ),
    "stopping": (
        "Plugin stopping; command not sent; give it again once the plugin has started",
        "Plugin stopt; opdracht niet verstuurd; geef hem opnieuw zodra de plugin weer draait",
    ),
    "not_authorized": (
        "Not signed in to Home Connect; use the sign-in link on the Home Connect device"
        " or in the Domoticz log",
        "Niet aangemeld bij Home Connect; gebruik de aanmeldlink op het Home Connect-apparaat"
        " of in het Domoticz-log",
    ),
    "offline": (
        "{a} is offline; check the power and Wi-Fi of the appliance",
        "{a} is offline; controleer de stroom en wifi van het apparaat",
    ),
    "unsupported": (
        "Command {command} is not supported here",
        "Opdracht {command} wordt hier niet ondersteund",
    ),
    "level_unavailable": (
        "That choice is not available; choose another",
        "Die keuze is niet beschikbaar; kies een andere",
    ),
    "not_a_number": ("Not a number; enter a number", "Geen getal; vul een getal in"),
    "choose_program": (
        "Choose a program; '-' selects nothing",
        "Kies een programma; '-' kiest niets",
    ),
    "choose_action": ("Choose an action; '-' does nothing", "Kies een actie; '-' doet niets"),
    "program_not_offered": (
        "{program} is not offered by {a}; choose another program",
        "{program} wordt niet aangeboden door {a}; kies een ander programma",
    ),
    "select_running": (
        "A program is running on {a}; stop it before choosing another",
        "Er loopt een programma op {a}; stop het voordat je een ander kiest",
    ),
    "select_off": (
        "{a} is switched off; switch it on first",
        "{a} staat uit; zet het eerst aan",
    ),
    "no_program_selected": (
        "No program is selected on {a}; choose one first",
        "Geen programma gekozen op {a}; kies er eerst een",
    ),
    "option_not_offered": (
        "{item} is not offered by the selected program; select a program that offers it",
        "{item} hoort niet bij het gekozen programma; kies een programma dat het aanbiedt",
    ),
    "option_running": (
        "{item} cannot be changed while the program runs; set it before the next start",
        "{item} kan niet worden gewijzigd terwijl het programma loopt; stel het in voor de"
        " volgende start",
    ),
    "value_not_allowed": (
        "{value} is not allowed for {item} now; allowed: {allowed}",
        "{value} is nu niet toegestaan voor {item}; toegestaan: {allowed}",
    ),
    "out_of_range": (
        "{value} is outside the allowed range for {item} ({range})",
        "{value} valt buiten het toegestane bereik voor {item} ({range})",
    ),
    "range_both": ("{low} to {high}", "{low} tot {high}"),
    "range_low": ("at least {low}", "minimaal {low}"),
    "range_high": ("at most {high}", "maximaal {high}"),
    "rounded": ("{item}: {value} rounded to {rounded}", "{item}: {value} afgerond op {rounded}"),
    "setting_read_only": (
        "{item} cannot be changed from Domoticz",
        "{item} kan niet vanuit Domoticz worden gewijzigd",
    ),
    "power_unknown": (
        "The power options of {a} are not known yet; try again later",
        "De aan/uit-mogelijkheden van {a} zijn nog niet bekend; probeer het later",
    ),
    "power_off_unsupported": (
        "{a} cannot be switched off remotely",
        "{a} kan niet op afstand worden uitgezet",
    ),
    "not_ready": (
        "{a} is not ready to start ({state}); wait until it is Ready",
        "{a} is niet klaar om te starten ({state}); wacht tot het Gereed is",
    ),
    "not_ready_off": (
        "{a} is not ready to start ({state}); switch it on first",
        "{a} is niet klaar om te starten ({state}); zet het eerst aan",
    ),
    "not_ready_finished": (
        "{a} is not ready to start ({state}); open the door or acknowledge the end first",
        "{a} is niet klaar om te starten ({state}); open eerst de deur of bevestig het einde",
    ),
    "not_ready_waiting": (
        "{a} is not ready to start ({state}); stop the program waiting for its delayed start first",
        "{a} is niet klaar om te starten ({state}); stop eerst het programma dat op de uitgestelde"
        " start wacht",
    ),
    "program_unknown": (
        "The program running on {a} is not known yet; try again shortly",
        "Het programma dat op {a} loopt is nog niet bekend; probeer het zo opnieuw",
    ),
    "nothing_to_stop": (
        "No program is running on {a}; there is nothing to stop",
        "Er loopt geen programma op {a}; er is niets te stoppen",
    ),
    "action_not_offered": (
        "{action} is not offered by {a}; use the appliance itself",
        "{action} wordt niet aangeboden door {a}; gebruik het apparaat zelf",
    ),
    "wrong_state": (
        "{action} is not possible on {a} now ({state}); it works only while {needed}",
        "{action} kan nu niet op {a} ({state}); dit kan alleen tijdens {needed}",
    ),
    "no_delay_option": (
        "The program offers no delayed start; choose '-' to start now",
        "Het programma heeft geen uitgestelde start; kies '-' om nu te starten",
    ),
    "delay_unknown": (
        "The time of the waiting program is not known yet; try again shortly",
        "De tijd van het wachtende programma is nog niet bekend; probeer het zo opnieuw",
    ),
    "delay_held": (
        "{time} chosen; it is sent with Start",
        "{time} gekozen; dit gaat mee met Start",
    ),
    "delay_cleared": (
        "Delayed start cleared; Start starts at once",
        "Uitgestelde start gewist; Start begint meteen",
    ),
    "delay_moved": (
        "Moving the delayed start to {time}",
        "Uitgestelde start wordt verzet naar {time}",
    ),
    "delay_now_running": (
        "The program waits for its delayed start; stop it and start again to start now",
        "Het programma wacht op de uitgestelde start; stop het en start opnieuw om nu te starten",
    ),
    "delay_too_early": (
        "{time} is too early for this program; the earliest is {earliest}",
        "{time} is te vroeg voor dit programma; het vroegst is {earliest}",
    ),
    "delay_clamped": (
        "{time} is later than the appliance allows; using {latest}",
        "{time} is later dan het apparaat toestaat; {latest} wordt gebruikt",
    ),
    "blocked": (
        "Home Connect asked to wait; try again in {seconds} s",
        "Home Connect vraagt te wachten; probeer het over {seconds} s",
    ),
    "brake": (
        "Paused after repeated errors; try again in {minutes} min",
        "Gepauzeerd na herhaalde fouten; probeer het over {minutes} min",
    ),
    "budget": (
        "Daily API budget used up; try again later",
        "Dagelijks API-budget op; probeer het later",
    ),
    "command_reserve": (
        "Daily API budget for commands used up; try again after {time}",
        "Dagelijks API-budget voor opdrachten op; probeer het na {time}",
    ),
    "network": (
        "Home Connect is not reachable; try again shortly",
        "Home Connect is niet bereikbaar; probeer het zo opnieuw",
    ),
    "limiter": (
        "{action} limit reached ({limit} per minute); try again in {seconds} s",
        "Limiet voor {action} bereikt ({limit} per minuut); probeer het over {seconds} s",
    ),
    "limiter_now": (
        "Start or stop limit reached ({limit} per minute); try again in a minute",
        "Limiet voor starten of stoppen bereikt ({limit} per minuut); probeer het over een minuut"
        " opnieuw",
    ),
    "limiter_wait": (
        "Start or stop limit reached ({limit} per minute); try again in {seconds} s",
        "Limiet voor starten of stoppen bereikt ({limit} per minuut); probeer het over {seconds} s",
    ),
    "pace": (
        "Too many requests this minute; try again in a minute",
        "Te veel verzoeken deze minuut; probeer het over een minuut opnieuw",
    ),
    "pace_wait": (
        "Too many requests this minute; try again in {seconds} s",
        "Te veel verzoeken deze minuut; probeer het over {seconds} s",
    ),
    "queue_full": (
        "Too many requests queued; try again in a minute",
        "Te veel verzoeken in de wachtrij; probeer het over een minuut opnieuw",
    ),
    "stale": (
        "Sign-in changed before it was sent; give the command again",
        "Aanmelding gewijzigd voordat het werd verstuurd; geef de opdracht opnieuw",
    ),
    "brake_now": (
        "Paused after repeated errors; try again in a few minutes",
        "Gepauzeerd na herhaalde fouten; probeer het over enkele minuten opnieuw",
    ),
    "blocked_now": (
        "Home Connect asked to slow down; try again in a minute",
        "Home Connect vraagt om rustiger aan te doen; probeer het over een minuut opnieuw",
    ),
    "forbidden": (
        "Not allowed for this Home Connect app; check the application on the developer portal",
        "Niet toegestaan voor deze Home Connect-app; controleer de applicatie op het developer"
        " portal",
    ),
    "refused_key": (
        "Home Connect refused it ({key}); check the appliance and try again",
        "Home Connect weigerde het ({key}); controleer het apparaat en probeer het opnieuw",
    ),
    "expired": (
        "Not sent within {seconds} s; dropped; give the command again",
        "Niet binnen {seconds} s verstuurd; vervallen; geef de opdracht opnieuw",
    ),
    "expired_stop": (
        "Not sent within {seconds} s: the Start before it was not confirmed yet; choose Stop again",
        "Niet binnen {seconds} s verstuurd: de Start ervoor was nog niet bevestigd; kies opnieuw"
        " Stop",
    ),
    "cancelled": ("Cancelled by Stop", "Geannuleerd door Stop"),
    "prerequisite": (
        "Start not sent: {what} failed; fix that first, then choose Start again",
        "Start niet verstuurd: {what} is mislukt; los dat eerst op en kies dan opnieuw Start",
    ),
    "unconfirmed": (
        "Not confirmed by {a}; the device shows what {a} reports",
        "Niet bevestigd door {a}; het apparaat toont wat {a} meldt",
    ),
    "start_not_taken": (
        "{a} did not start; check the appliance (for example the water tank or the door)",
        "{a} is niet gestart; controleer het apparaat (bijvoorbeeld het waterreservoir of de deur)",
    ),
    "unchanged": (
        "No change ({command}); nothing sent",
        "Geen wijziging ({command}); niets verstuurd",
    ),
    "on": ("On", "Aan"),
    "off": ("Off", "Uit"),
}
# On-screen subjects per command event, (English, Dutch); the event's reason is the text.
_SCREEN = {
    "command_refused": ("{device}: not sent", "{device}: niet verstuurd"),
    "command_failed": ("{a}: {command} failed", "{a}: {command} mislukt"),
    "command_expired": ("{a}: {command} not sent", "{a}: {command} niet verstuurd"),
    "command_unconfirmed": ("{a}: {command} not confirmed", "{a}: {command} niet bevestigd"),
    "command_confirmed": ("{a}: {command} done", "{a}: {command} gelukt"),
}
# A confirmation carries no reason, so it gets this text.
_CONFIRMED = ("Confirmed by {a}", "Bevestigd door {a}")
# Worker refusal reasons (workers.Refused) to message names.
_REFUSED = {
    "brake": "brake_now",
    "blocked": "blocked_now",
    "total": "budget",
    "ceiling": "budget",
    "limiter": "limiter_now",
    "pace": "pace",
    "queue_full": "queue_full",
    "stale": "stale",
    "no_token": "not_authorized",
}
# Worker refusals whose wait the budget knows, to the message that names it.
_REFUSED_WAIT = {
    "brake": "brake",
    "blocked": "blocked",
    "limiter": "limiter_wait",
    "pace": "pace_wait",
}
# Home Connect state errors on a write, to the decide-time message naming the same fix.
_NO_PROGRAM_ACTIVE = "SDK.Error.NoProgramActive"
_WRONG_STATE = "SDK.Error.WrongOperationState"
_NOT_AVAILABLE = "SDK.Error.ProgramNotAvailable"
_NOT_SELECTED = "SDK.Error.SelectedProgramNotSet"


def message(name: str, lang: str, **values: object) -> str:
    english, dutch = _TEXT[name]
    # str.format ignores values a template does not use.
    return (dutch if lang == "nl" else english).format(**values)


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    # The user's values: kept out of the repr so a logged Request does not echo them.
    body: dict | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Intent:
    """One write for the coordinator.

    action: select, option, setting, start, stop, command or delay. request is None only for a
    Stop that has nothing to stop and only cancels a Start waiting to be sent. overlay is
    (unit, nvalue, svalue) shown at once while the write is pending (spec 7.6). coalesce: a
    queued write with the same key is replaced by this one ("" never coalesces; Start and Stop
    never do). verify: the model kind a confirming GET fetches ("status", "settings",
    "selected", "active"), or "" when the answer is all there is to know. confirm_by_get: only
    that GET confirms, never an event (a selection: its GET also brings the new program's
    options). note: an adjustment to log (a rounded value). follows_start: a Stop given while
    nothing is seen running but a sent Start is not settled; it goes only if that Start was
    accepted (spec 7.6: Stop needs an active program).
    """

    ha_id: str
    action: str
    key: str
    value: object
    request: Request | None
    describe: str
    overlay: tuple[int, int, str] | None = None
    coalesce: str = ""
    debounce: bool = False
    limit: str = ""
    verify: str = ""
    confirm_by_get: bool = False
    note: str = ""
    follows_start: bool = False


@dataclass(frozen=True)
class Refusal:
    """Nothing is sent; the plugin logs `reason` as `event` at `level`."""

    reason: str
    level: str = "status"
    event: str = "command_refused"


@dataclass(frozen=True)
class Delay:
    seconds: int
    note: str = ""


@dataclass(frozen=True)
class Range:
    low: float | None
    high: float | None
    step: float | None


# -- requests ------------------------------------------------------------------------------------


def _path(ha_id: str, *tail: str) -> str:
    return hc_client.api_path("homeappliances", ha_id, *tail)


def _data(key: str, value: object, unit: str = "") -> dict:
    data: dict = {"key": key, "value": value}
    if unit:
        data["unit"] = unit
    return {"data": data}


def select_request(ha_id: str, program: str) -> Request:
    return Request("PUT", _path(ha_id, "programs", "selected"), {"data": {"key": program}})


def option_request(
    ha_id: str, key: str, value: object, unit: str = "", *, running: bool = False
) -> Request:
    target = "active" if running else "selected"
    return Request("PUT", _path(ha_id, "programs", target, "options", key), _data(key, value, unit))


def setting_request(ha_id: str, key: str, value: object) -> Request:
    return Request("PUT", _path(ha_id, "settings", key), _data(key, value))


def start_request(ha_id: str, program: str, options: list[dict]) -> Request:
    data: dict = {"key": program}
    if options:
        data["options"] = options
    return Request("PUT", _path(ha_id, "programs", "active"), {"data": data})


def stop_request(ha_id: str) -> Request:
    return Request("DELETE", _path(ha_id, "programs", "active"))


def command_request(ha_id: str, key: str) -> Request:
    return Request("PUT", _path(ha_id, "commands", key), _data(key, True))


# -- values --------------------------------------------------------------------------------------


def as_number(level: object) -> float | None:
    """A Domoticz command level as a finite number, or None."""
    if isinstance(level, bool) or not isinstance(level, int | float | str):
        return None
    try:
        number = float(level)
    except (ValueError, OverflowError):
        return None
    return planner.finite_number(number)


def value_range(constraints: dict, declared: str) -> Range:
    step = planner.finite_number(constraints.get("stepsize"))
    if step is not None and step <= 0:
        step = None
    if step is None and declared == "Int":
        step = 1.0
    return Range(
        planner.finite_number(constraints.get("min")),
        planner.finite_number(constraints.get("max")),
        step,
    )


def in_range(raw: float, rng: Range) -> bool:
    return (rng.low is None or raw >= rng.low) and (rng.high is None or raw <= rng.high)


def snap(raw: float, rng: Range) -> float:
    """The nearest step counted from min, never past max (spec 6.3)."""
    if rng.step is None:
        return raw
    base = rng.low if rng.low is not None else 0.0
    value = base + round((raw - base) / rng.step) * rng.step
    if rng.high is not None and value > rng.high:
        value -= rng.step
    return round(value, 6)


def wire_number(value: float, declared: str) -> int | float:
    return int(value) if declared == "Int" or value == int(value) else value


def _shown_range(rng: Range, scale: int) -> Range:
    """The range in the card's unit, as the planner writes ValueMin and ValueMax."""
    low = planner.scaled_bound(rng.low, scale, upper=False) if rng.low is not None else None
    high = planner.scaled_bound(rng.high, scale, upper=True) if rng.high is not None else None
    return Range(low, high, None)


def range_text(rng: Range, unit: str, lang: str) -> str:
    suffix = f" {unit}" if unit else ""
    low = planner._fmt(rng.low) + suffix if rng.low is not None else ""
    high = planner._fmt(rng.high) + suffix if rng.high is not None else ""
    if low and high:
        return message("range_both", lang, low=low, high=high)
    if low:
        return message("range_low", lang, low=low)
    return message("range_high", lang, high=high)


# -- program details -----------------------------------------------------------------------------


def program_option(app: model.Appliance, program: str, key: str) -> dict | None:
    return next(
        (
            o
            for o in model.option_list(app.cache.program_details.get(program))
            if o.get("key") == key
        ),
        None,
    )


def writable(detail: dict | None) -> bool:
    """An option without an access field is writable, as model.Appliance.capability reads it."""
    return (
        detail is not None
        and planner.constraints_of(detail).get("access", "readWrite") == "readWrite"
    )


def start_options(app: model.Appliance, program: str) -> list[dict]:
    """The observed values of the program's options that are selectable before a start: never a
    runtime value and never a start-only one (spec 6.3, 7.6)."""
    out = []
    for option in model.option_list(app.cache.program_details.get(program)):
        key = option.get("key")
        if not text.valid_key(key) or key in model.START_ONLY or not writable(option):
            continue
        observed = app.options.get(key)
        if observed is None or observed.value is None or isinstance(observed.value, dict | list):
            continue
        out.append({"key": key, "value": observed.value})
    return out


def delay_option_of(app: model.Appliance, program: str) -> str | None:
    keys = {o.get("key") for o in model.option_list(app.cache.program_details.get(program))}
    return planner.pick_delay_key(keys)


# -- delayed start -------------------------------------------------------------------------------


def next_time(minutes: int, wall: float) -> float:
    """Wall time of the next occurrence of a local clock time; one already passed, or closer
    than DELAY_MIN_LEAD_S, is tomorrow's. mktime normalises the day overflow and DST itself."""
    local = time.localtime(wall)

    def at(days: int) -> float:
        hour, minute = divmod(minutes, 60)
        return time.mktime(
            (local.tm_year, local.tm_mon, local.tm_mday + days, hour, minute, 0, 0, 0, -1)
        )

    today = at(0)
    return today if today > wall + DELAY_MIN_LEAD_S else at(1)


def _earliest_slot(epoch: float) -> str:
    """The first selectable delayed-start time at or after `epoch`: the user can only pick
    times on the planner's grid, so naming a time between slots would name one that is refused."""
    local = time.localtime(epoch)
    minutes = local.tm_hour * 60 + local.tm_min + (1 if local.tm_sec else 0)
    slot = -(-minutes // planner.DELAY_STEP_MIN) * planner.DELAY_STEP_MIN
    return planner.clock_text(slot % (24 * 60))


def delay_seconds(minutes: int, constraints: dict, wall: float, lang: str) -> Delay | Refusal:
    """The relative seconds Home Connect expects for a delay to a clock time (spec 6.3).

    Beyond the option's max it is clamped with a note (the one clamp spec 6.3 allows); below its
    min it is refused naming the earliest time, because moving a finish time silently is what
    spec 6.3 rejects for every other value. Steps round down, so the chosen time is never
    overshot.
    """
    seconds = int(next_time(minutes, wall) - wall)
    rng = value_range(constraints, "Int")
    note = ""
    if rng.high is not None and seconds > rng.high:
        seconds = int(rng.high)
        note = message(
            "delay_clamped",
            lang,
            time=planner.clock_text(minutes),
            latest=planner.clock(wall + seconds),
        )
    if rng.step is not None and rng.step > 1:
        base = rng.low if rng.low is not None else 0.0
        seconds = int(base + math.floor((seconds - base) / rng.step) * rng.step)
    if rng.low is not None and seconds < rng.low:
        return Refusal(
            message(
                "delay_too_early",
                lang,
                time=planner.clock_text(minutes),
                earliest=_earliest_slot(wall + rng.low),
            )
        )
    return Delay(seconds, note)


def spent_delays(
    previous: dict[str, str], apps: Iterable[model.Appliance], held: dict[str, int]
) -> list[str]:
    """haIds whose held delayed-start time was used up: the appliance left DelayedStart, so the
    time applied to that run only. Updates `previous` (last known state per haId) and `held`;
    an unknown state changes nothing."""
    spent = []
    for app in apps:
        state = app.operation_state()
        if state is None:
            continue
        was = previous.get(app.ha_id)
        previous[app.ha_id] = state
        if (
            was == "DelayedStart"
            and state != "DelayedStart"
            and held.pop(app.ha_id, None) is not None
        ):
            spent.append(app.ha_id)
    return spent


# -- outcomes ------------------------------------------------------------------------------------


def _same(observed: model.Value | None, wanted: object) -> bool:
    if observed is None:
        return False
    a, b = planner.finite_number(observed.value), planner.finite_number(wanted)
    if a is not None and b is not None:
        return abs(a - b) < 1e-6
    return observed.value == wanted


def confirmed(intent: Intent, app: model.Appliance | None, since: int) -> bool:
    """True once the model holds an observation newer than the write (`since` is the sequence
    number it was sent at) of what the write asked for. The model only holds observed values
    (spec 7.6), so this is the only path from accepted to observed."""
    if app is None:
        return False

    def newer(key: str) -> bool:
        return app.seq.get(key, 0) > since

    state = app.operation_state()
    if intent.action == "select":
        return newer(model.SELECTED_ROOT) and app.selected_program == intent.key
    if intent.action == "option":
        return newer(intent.key) and _same(app.options.get(intent.key), intent.value)
    if intent.action == "setting":
        return newer(intent.key) and _same(app.settings.get(intent.key), intent.value)
    if intent.action == "start":
        return newer(model.OPERATION_STATE) and state in ("Run", "DelayedStart")
    if intent.action == "stop":
        return newer(model.OPERATION_STATE) and state not in RUNNING_STATES
    if intent.action == "command":
        target = COMMAND_STATES.get(intent.key)
        return target is not None and newer(model.OPERATION_STATE) and state == target[1]
    return False


def failure_reason(category: str, error_key: str, description: str, label: str, lang: str) -> str:
    """Why Home Connect refused a write, as the user should read it (spec 7.1)."""
    known = errors.user_message(error_key, label, lang) if error_key else None
    if known:
        return known
    if category == errors.OFFLINE:
        return message("offline", lang, a=label)
    if category == errors.LIMITED:
        return message("blocked_now", lang)
    if category == errors.FORBIDDEN:
        return message("forbidden", lang)
    if category == errors.REFUSED_VALIDATION and description:
        # Home Connect's own words (spec 7.1). Untrusted text: cleaned and bounded; the log
        # emitter quotes it.
        return text.clean_label(description, _REASON_MAX)
    return message("refused_key", lang, key=text.clean_label(error_key or category or "?", 80))


def answer_reason(
    intent: Intent, error_key: str, app: model.Appliance | None, label: str, lang: str
) -> str | None:
    """The fix for a state Home Connect refused a write in, as decide() names it when the model
    shows that state first; None when the model cannot tell which fix applies."""
    action = intent.action
    if error_key == _NO_PROGRAM_ACTIVE and action in ("stop", "command"):
        return message("nothing_to_stop", lang, a=label)
    if error_key == _NOT_SELECTED:
        return message("no_program_selected", lang, a=label)
    if app is None:
        return None
    if error_key == _NOT_AVAILABLE and action in ("select", "start"):
        program = planner.program_label(app, intent.key, lang)
        return message("program_not_offered", lang, program=program, a=label)
    state = app.operation_state()
    if error_key != _WRONG_STATE or state is None:
        return None
    if action == "select" and state in RUNNING_STATES:
        return message("select_running", lang, a=label)
    shown = catalog.state_name(state, lang)
    if action == "start" and state != "Ready":
        return message(_NOT_READY.get(state, "not_ready"), lang, a=label, state=shown)
    states = COMMAND_STATES.get(intent.key)
    if action == "command" and states is not None and state != states[0]:
        needed = catalog.state_name(states[0], lang)
        return message(
            "wrong_state", lang, action=intent.describe, a=label, state=shown, needed=needed
        )
    return None


def shows_on_screen(event: str, choice: str) -> bool:
    """Whether a command event goes on screen under the On-screen messages choice."""
    if event not in _SCREEN or choice == "off":
        return False
    return choice == "all" or event != "command_confirmed"


def screen_message(
    event: str, device: str, label: str, command: str, reason: str, lang: str
) -> tuple[str, str] | None:
    """The (subject, text) a command event shows on screen, or None for an event that has none.
    A refusal names the device it was given on; the others name the appliance and command."""
    subjects = _SCREEN.get(event)
    if subjects is None:
        return None
    nl = lang == "nl"
    subject = subjects[nl].format(device=device, a=label, command=command)
    text = _CONFIRMED[nl].format(a=label) if event == "command_confirmed" else reason
    return subject, text


def refused_reason(reason: str, lang: str, wait_s: float = 0.0) -> str:
    """Why the worker did not send a write (workers.Refused), with the wait when it is known."""
    name = (_REFUSED_WAIT.get(reason) if wait_s > 0 else None) or _REFUSED.get(reason)
    if name is None:
        return message("refused_key", lang, key=text.clean_label(reason, 40))
    seconds, minutes = math.ceil(wait_s), max(1, math.ceil(wait_s / 60))
    return message(name, lang, limit=budget.ACTION_LIMIT, seconds=seconds, minutes=minutes)


# -- jobs ----------------------------------------------------------------------------------------


def write_job(write_id: int, intent: Intent, issued: int, deadline: float) -> scheduler.Job:
    request = intent.request
    if request is None:
        raise ValueError("a Stop that only cancels is never sent")
    return scheduler.Job(
        "command",
        request.method,
        request.path,
        intent.ha_id,
        intent.key,
        budget.COMMAND,
        scheduler.PRIORITY_COMMAND,
        issued_seq=issued,
        ttl_deadline=deadline,
        body=request.body,
        command_id=write_id,
        limit=intent.limit,
    )


def verify_job(write_id: int, intent: Intent, issued: int, deadline: float) -> scheduler.Job:
    """The reconciliation GET of a write (spec 7.6). Its key names the model kind the answer
    is applied as; it is booked as a command (spec 7.7)."""
    return scheduler.Job(
        "verify",
        "GET",
        _path(intent.ha_id, *_VERIFY_PATHS[intent.verify]),
        intent.ha_id,
        intent.verify,
        budget.COMMAND,
        scheduler.PRIORITY_COMMAND,
        issued_seq=issued,
        ttl_deadline=deadline,
        command_id=write_id,
    )


# -- decisions -----------------------------------------------------------------------------------

_READ_ONLY_UNITS = frozenset(
    {
        planner.UNIT_STATE,
        planner.UNIT_DOOR,
        planner.UNIT_PROGRESS,
        planner.UNIT_TIME,
        planner.UNIT_PROGRAM,
        planner.UNIT_REMOTE,
        planner.UNIT_STATUS_CODE,
    }
)
_ACTION_COMMANDS = {
    "pause": catalog.PAUSE,
    "resume": catalog.RESUME,
    "open_door": catalog.OPEN_DOOR,
    "partly_open_door": catalog.PARTLY_OPEN_DOOR,
}
# Appliance types the docs list each command for (spec 3.2a). The cached /commands list is
# refreshed daily, not on a state change, so a command missing from it can still be valid now.
# The door commands are absent: only an oven with a motorised door has them, so only the
# appliance's own list offers them.
_COMMAND_TYPES = {
    catalog.PAUSE: frozenset(
        {"Microwave", "Oven", "Washer", "Dryer", "WasherDryer", "CleaningRobot"}
    ),
    catalog.RESUME: frozenset(
        {"Microwave", "Oven", "Washer", "Dryer", "WasherDryer", "CleaningRobot", "Dishwasher"}
    ),
}
# Unit kinds a command can write; any other kind shows a runtime or read-only value.
_WRITABLE_KINDS = frozenset({"switch", "selector", "setpoint"})
_POWER_ON = "BSH.Common.EnumType.PowerState.On"
# Off where the appliance has it, else Standby; both display as off (spec 6.2 unit 2).
_POWER_OFF = ("BSH.Common.EnumType.PowerState.Off", "BSH.Common.EnumType.PowerState.Standby")
# Unmet start preconditions (spec 3.2a) and the refusal whose message names their fix.
_START_BLOCKERS = (
    (catalog.REMOTE_ACTIVE, False, "BSH.Common.Error.RemoteControlNotActive"),
    (catalog.REMOTE_START, False, "BSH.Common.Error.RemoteStartNotActive"),
    (catalog.LOCAL_CONTROL, True, "BSH.Common.Error.LockedByLocalControl"),
)
_DOOR_OPEN = "BSH.Common.EnumType.DoorState.Open"
_WRONG_DOOR = "SDK.Error.WrongDoorState"


@dataclass(frozen=True)
class Hold:
    """A delayed-start time the plugin holds and sends with Start (spec 6.3).

    Without `intent` the caller holds `minutes` at once (None clears it). With `intent` the
    program already waits for its delayed start and the intent moves it: the caller holds
    `minutes` only once that write is confirmed, and keeps the old time held and shown when it
    is refused or fails (spec 6.3). decide() itself never changes State.
    """

    minutes: int | None
    note: str
    intent: Intent | None = None


@dataclass(frozen=True)
class Context:
    app: model.Appliance | None
    state: persistence.State
    label: str
    lang: str
    allow_control: bool
    authorized: bool
    snapshot: budget.BudgetSnapshot
    mono: float
    wall: float
    # The program of a selection not confirmed yet, whether a Start waits to be sent, and
    # whether one was sent and is not settled yet: a scene's later commands are judged by what
    # its earlier ones asked for.
    pending_program: str | None = None
    pending_start: bool = False
    sent_start: bool = False
    # Option and setting keys with a write not settled yet.
    pending_keys: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _Chosen:
    wire: object
    shown: str
    nvalue: int
    svalue: str
    number: bool = False
    note: str = ""


def decide(
    ha_id: str, unit: int, command: str, level: object, ctx: Context
) -> Intent | Refusal | Hold:
    """One onCommand on an appliance unit (spec 7.6). With AllowControl off everything is
    refused before anything else, hidden devices and the program selector included."""
    lang = ctx.lang
    if not ctx.allow_control:
        return Refusal(message("control_off", lang))
    if unit in _READ_ONLY_UNITS:
        return Refusal(message("read_only", lang))
    app = ctx.app
    if app is None:
        return Refusal(message("unknown_appliance", lang))
    if unit == planner.UNIT_DELAYED_START:
        # Checked before authorization: a held time sends nothing until Start.
        return _delay(ctx, app, command, level)
    if not ctx.authorized:
        return Refusal(message("not_authorized", lang))
    if not app.connected:
        return Refusal(message("offline", lang, a=ctx.label))
    if unit == planner.UNIT_POWER:
        return _power(ctx, app, command)
    if unit == planner.UNIT_PROGRAM_SELECT:
        return _select(ctx, app, command, level)
    if unit == planner.UNIT_ACTIONS:
        return _action(ctx, app, command, level)
    if unit == planner.UNIT_ACK:
        if command != "On":
            return _unsupported(command, lang)
        return _command(ctx, app, catalog.ACKNOWLEDGE, catalog.word("acknowledge", lang))
    allocated = ctx.state.unit_alloc.get(ha_id, {})
    key = next((k for k, number in allocated.items() if number == unit), None)
    if key is not None and ".Option." in key:
        return _option(ctx, app, unit, key, command, level)
    if key is not None and ".Setting." in key:
        return _setting(ctx, app, unit, key, command, level)
    return Refusal(message("read_only", lang))


def _unchanged(
    ctx: Context, key: str, observed: model.Value | None, wanted: object, describe: str
) -> Refusal | None:
    """A write of the value already observed sends nothing and spends no budget. Not while a
    write to the same key is pending: it may still change the value."""
    if key in ctx.pending_keys or not _same(observed, wanted):
        return None
    return Refusal(message("unchanged", ctx.lang, command=describe), "debug", "command_skipped")


def _unsupported(command: str, lang: str) -> Refusal:
    return Refusal(message("unsupported", lang, command=text.clean_label(command, 20)))


def _running(app: model.Appliance) -> bool:
    state = app.operation_state()
    return state in RUNNING_STATES or (state is None and app.active_program is not None)


def _level_index(level: object, size: int) -> int | None:
    """Index into a level list for a selector level (10, 20, ...), or None."""
    number = as_number(level)
    if number is None or number <= 0 or number % 10:
        return None
    index = int(number) // 10 - 1
    return index if index < size else None


def _held_back(ctx: Context, action: str = "") -> Refusal | None:
    """The budget conditions under which the worker would refuse anyway (spec 7.6), from the
    snapshot. A brake window that has passed is let through: the worker may send it as the
    probe (spec 7.2)."""
    snap, mono, lang = ctx.snapshot, ctx.mono, ctx.lang
    if snap.block_until_mono > mono:
        return Refusal(message("blocked", lang, seconds=math.ceil(snap.block_until_mono - mono)))
    if snap.braked and snap.brake_until_mono > mono:
        minutes = max(1, math.ceil((snap.brake_until_mono - mono) / 60))
        return Refusal(message("brake", lang, minutes=minutes))
    if snap.total_used >= budget.TOTAL:
        return Refusal(message("budget", lang))
    if snap.command_free_at:
        # The rest of the total is kept for authorization and the stream (spec 7.7).
        return Refusal(message("command_reserve", lang, time=planner.clock(snap.command_free_at)))
    if snap.network_failed_mono and mono - snap.network_failed_mono < NETWORK_BACKOFF_S:
        return Refusal(message("network", lang))
    free_at = snap.action_free_at.get(action, 0.0) if action else 0.0
    if free_at > mono:
        name = catalog.word(f"action_{action}", lang)
        seconds = math.ceil(free_at - mono)
        return Refusal(
            message("limiter", lang, action=name, limit=budget.ACTION_LIMIT, seconds=seconds)
        )
    return None


def _value(
    ctx: Context,
    app: model.Appliance,
    unit: int,
    command: str,
    level: object,
    detail: dict,
    item: str,
) -> _Chosen | Refusal:
    """The value a command on an option or setting unit asks for, by the unit's kind."""
    lang = ctx.lang
    kind = ctx.state.unit_kinds.get(app.ha_id, {}).get(str(unit), "")
    if kind == "switch":
        if command not in ("On", "Off"):
            return _unsupported(command, lang)
        on = command == "On"
        return _Chosen(on, message("on" if on else "off", lang), int(on), "On" if on else "Off")
    if kind == "selector":
        if command not in ("Set Level", "Off"):
            return _unsupported(command, lang)
        keys = ctx.state.level_maps.get(app.ha_id, {}).get(str(unit), [])
        index = _level_index(0 if command == "Off" else level, len(keys))
        if index is None:
            return Refusal(message("level_unavailable", lang))
        value = keys[index]
        shown = planner.value_label(app, value, lang)
        allowed = planner.constraints_of(detail).get("allowedvalues")
        if isinstance(allowed, list) and value not in allowed:
            names = [planner.value_label(app, v, lang) for v in allowed if text.valid_key(v)]
            listed = text.clean_label(", ".join(names) or "-", _REASON_MAX)
            return Refusal(
                message("value_not_allowed", lang, value=shown, item=item, allowed=listed)
            )
        level_value = 10 * (index + 1)
        return _Chosen(value, shown, level_value, str(level_value))
    if kind == "setpoint":
        if command != "Set Level":
            return _unsupported(command, lang)
        typed = as_number(level)
        if typed is None:
            return Refusal(message("not_a_number", lang))
        declared = str(detail.get("type") or "")
        rng = value_range(planner.constraints_of(detail), declared)
        unit_label = str(detail.get("unit") or "")
        # The card shows seconds as minutes (planner.setpoint_scale); range and step stay in the
        # wire unit, and every number the user reads is converted back to the card's unit.
        scale = planner.setpoint_scale(unit_label)
        unit_text = text.clean_label(planner.setpoint_unit(unit_label, lang), 20)
        # Rounded so a typed 4.1 min is 246 s, not 245.99999999999997, which would be "rounded".
        raw = round(typed * scale, 6) if scale > 1 else typed
        if not in_range(raw, rng):
            span = range_text(_shown_range(rng, scale), unit_text, lang)
            return Refusal(
                message("out_of_range", lang, value=planner._fmt(typed), item=item, range=span)
            )
        snapped = snap(raw, rng)
        # The planner's sValue for a reported setpoint, so the overlay and the next report agree.
        svalue = planner.setpoint_svalue(snapped, scale)
        note = ""
        if snapped != raw:
            note = message(
                "rounded",
                lang,
                item=item,
                value=planner._fmt(typed),
                rounded=planner._fmt(float(svalue)),
            )
        shown = f"{planner._fmt(float(svalue))} {unit_text}".strip()
        wire = wire_number(snapped, declared)
        return _Chosen(wire, shown, 0, svalue, number=True, note=note)
    return Refusal(message("read_only", lang))


def _option(
    ctx: Context, app: model.Appliance, unit: int, key: str, command: str, level: object
) -> Intent | Refusal:
    lang = ctx.lang
    item = catalog.name_for(app.names, key, lang)
    if key in model.START_ONLY:
        # Held by the plugin and sent only with Start (spec 6.3); never a device of its own.
        return Refusal(message("read_only", lang))
    # Before the program's details: a runtime value one program does not offer as writable is
    # read-only on every program, so "not offered" would name a fix that does not exist.
    if ctx.state.unit_kinds.get(app.ha_id, {}).get(str(unit)) not in _WRITABLE_KINDS:
        return Refusal(message("read_only", lang))
    running = _running(app)
    program = app.active_program if running else (ctx.pending_program or app.selected_program)
    if program is None:
        name = "program_unknown" if running else "no_program_selected"
        return Refusal(message(name, lang, a=ctx.label))
    detail = program_option(app, program, key)
    if detail is None or not writable(detail):
        return Refusal(message("option_not_offered", lang, item=item))
    if running and planner.constraints_of(detail).get("liveupdate") is not True:
        return Refusal(message("option_running", lang, item=item))
    chosen = _value(ctx, app, unit, command, level, detail, item)
    if isinstance(chosen, Refusal):
        return chosen
    describe = f"{item}: {chosen.shown}"
    # The observed value counts only once reported for the program it would change: not while
    # a selection is pending, nor when it was reported before the current program was.
    root = model.ACTIVE_ROOT if running else model.SELECTED_ROOT
    if ctx.pending_program is None and app.seq.get(key, 0) > app.seq.get(root, 0):
        same = _unchanged(ctx, key, app.options.get(key), chosen.wire, describe)
        if same is not None:
            return same
    held = _held_back(ctx)
    if held is not None:
        return held
    unit_text = str(detail.get("unit") or "") if chosen.number else ""
    return Intent(
        app.ha_id,
        "option",
        key,
        chosen.wire,
        option_request(app.ha_id, key, chosen.wire, unit_text, running=running),
        describe,
        overlay=(unit, chosen.nvalue, chosen.svalue),
        coalesce=f"option:{key}",
        debounce=chosen.number,
        verify="active" if running else "selected",
        note=chosen.note,
    )


def _setting(
    ctx: Context, app: model.Appliance, unit: int, key: str, command: str, level: object
) -> Intent | Refusal:
    lang = ctx.lang
    item = catalog.name_for(app.names, key, lang)
    if app.capability(key) != "setting_rw":
        return Refusal(message("setting_read_only", lang, item=item))
    # A setting's unit arrives only with its value; the planner shows the card in that unit.
    detail = {
        "constraints": app.cache.setting_constraints.get(key, {}),
        "unit": planner.key_unit(app.settings.get(key), {}),
    }
    chosen = _value(ctx, app, unit, command, level, detail, item)
    if isinstance(chosen, Refusal):
        return chosen
    describe = f"{item}: {chosen.shown}"
    held = _unchanged(ctx, key, app.settings.get(key), chosen.wire, describe) or _held_back(ctx)
    if held is not None:
        return held
    return Intent(
        app.ha_id,
        "setting",
        key,
        chosen.wire,
        setting_request(app.ha_id, key, chosen.wire),
        describe,
        overlay=(unit, chosen.nvalue, chosen.svalue),
        coalesce=f"setting:{key}",
        debounce=chosen.number,
        verify="settings",
        note=chosen.note,
    )


def _power(ctx: Context, app: model.Appliance, command: str) -> Intent | Refusal:
    lang = ctx.lang
    if command not in ("On", "Off"):
        return _unsupported(command, lang)
    key = catalog.POWER_STATE
    item = catalog.word("power", lang)
    constraints = app.cache.setting_constraints.get(key)
    if constraints is None:
        return Refusal(message("power_unknown", lang, a=ctx.label))
    if app.capability(key) != "setting_rw":
        return Refusal(message("setting_read_only", lang, item=item))
    on = command == "On"
    allowed = constraints.get("allowedvalues")
    offered = allowed if isinstance(allowed, list) else []
    value = _POWER_ON if on else next((v for v in _POWER_OFF if v in offered), None)
    if value is None:
        return Refusal(message("power_off_unsupported", lang, a=ctx.label))
    describe = f"{item}: {message('on' if on else 'off', lang)}"
    held = _unchanged(ctx, key, app.settings.get(key), value, describe) or _held_back(ctx)
    if held is not None:
        return held
    return Intent(
        app.ha_id,
        "setting",
        key,
        value,
        setting_request(app.ha_id, key, value),
        describe,
        overlay=(planner.UNIT_POWER, int(on), "On" if on else "Off"),
        coalesce=f"setting:{key}",
        verify="settings",
    )


def _select(ctx: Context, app: model.Appliance, command: str, level: object) -> Intent | Refusal:
    lang = ctx.lang
    if command not in ("Set Level", "Off"):
        return _unsupported(command, lang)
    if command == "Off" or as_number(level) == 0:
        return Refusal(message("choose_program", lang))
    keys = ctx.state.level_maps.get(app.ha_id, {}).get(str(planner.UNIT_PROGRAM_SELECT), [])
    index = _level_index(level, len(keys))
    if index is None:
        return Refusal(message("level_unavailable", lang))
    program = keys[index]
    name = planner.program_label(app, program, lang)
    # A program that disappeared keeps its level and name; choosing it is refused (spec 6.3).
    if program not in app.cache.programs and program != app.selected_program:
        return Refusal(message("program_not_offered", lang, program=name, a=ctx.label))
    if _running(app):
        return Refusal(message("select_running", lang, a=ctx.label))
    # An appliance that reports itself off answers a selection with a bare 400.BadRequest; one
    # that does not report its power, or has a power write pending (a scene's Power On before
    # its program), gets the selection sent.
    power = app.settings.get(catalog.POWER_STATE)
    switching = catalog.POWER_STATE in ctx.pending_keys
    if power is not None and power.value in _POWER_OFF and not switching:
        return Refusal(message("select_off", lang, a=ctx.label))
    held = _held_back(ctx)
    if held is not None:
        return held
    level_value = 10 * (index + 1)
    return Intent(
        app.ha_id,
        "select",
        program,
        program,
        select_request(app.ha_id, program),
        f"{catalog.word('program_select', lang)}: {name}",
        overlay=(planner.UNIT_PROGRAM_SELECT, level_value, str(level_value)),
        coalesce="select",
        verify="selected",
        confirm_by_get=True,
    )


def _action(ctx: Context, app: model.Appliance, command: str, level: object) -> Intent | Refusal:
    lang = ctx.lang
    if command == "Off" or as_number(level) == 0:
        return Refusal(message("choose_action", lang))
    if command != "Set Level":
        return _unsupported(command, lang)
    number = as_number(level)
    action = next((name for name, value in planner.ACTION_LEVELS.items() if value == number), None)
    if action is None:
        return Refusal(message("level_unavailable", lang))
    if action == "start":
        return _start(ctx, app)
    if action == "stop":
        return _stop(ctx, app)
    return _command(ctx, app, _ACTION_COMMANDS[action], catalog.word(f"action_{action}", lang))


def _command(ctx: Context, app: model.Appliance, key: str, name: str) -> Intent | Refusal:
    lang, label = ctx.lang, ctx.label
    if key not in app.cache.commands and app.type not in _COMMAND_TYPES.get(key, frozenset()):
        return Refusal(message("action_not_offered", lang, action=name, a=label))
    states = COMMAND_STATES.get(key)
    state = app.operation_state()
    if states is not None and state is not None and state != states[0]:
        shown = catalog.state_name(state, lang)
        needed = catalog.state_name(states[0], lang)
        return Refusal(
            message("wrong_state", lang, action=name, a=label, state=shown, needed=needed)
        )
    held = _held_back(ctx)
    if held is not None:
        return held
    return Intent(
        app.ha_id,
        "command",
        key,
        True,
        command_request(app.ha_id, key),
        name,
        verify="status" if states else "",
    )


def build_start(
    app: model.Appliance | None,
    held: int | None,
    wall: float,
    lang: str,
    label: str,
    program: str | None = None,
) -> tuple[Request, str] | Refusal:
    """The Start request and a note to log, or why it cannot go (spec 7.6).

    Run when the command is given, with a selection still pending as `program`, and again when
    it is sent, on the program selected by then: its option values are the ones observed after
    every earlier write, and the delay counts from that moment (spec 6.3). A precondition the
    appliance does not report counts as met.
    """
    if app is None:
        return Refusal(message("unknown_appliance", lang))
    program = program or app.selected_program
    for key, unmet, error_key in _START_BLOCKERS:
        observed = app.status.get(key)
        if observed is not None and observed.value is unmet:
            return Refusal(errors.user_message(error_key, label, lang) or error_key)
    state = app.operation_state()
    if state is not None and state != "Ready":
        shown = catalog.state_name(state, lang)
        return Refusal(message(_NOT_READY.get(state, "not_ready"), lang, a=label, state=shown))
    # After the state: a finished appliance's own fix (open the door, acknowledge) comes first.
    door = app.status.get(catalog.DOOR_STATE)
    if door is not None and door.value == _DOOR_OPEN:
        return Refusal(errors.user_message(_WRONG_DOOR, label, lang) or _WRONG_DOOR)
    if program is None:
        return Refusal(message("no_program_selected", lang, a=label))
    options = start_options(app, program)
    note = ""
    if held is not None:
        key = delay_option_of(app, program)
        if key is None:
            return Refusal(message("no_delay_option", lang))
        constraints = planner.constraints_of(program_option(app, program, key))
        delay = delay_seconds(held, constraints, wall, lang)
        if isinstance(delay, Refusal):
            return delay
        options.append({"key": key, "value": delay.seconds})
        note = delay.note
    return start_request(app.ha_id, program, options), note


def _start(ctx: Context, app: model.Appliance) -> Intent | Refusal:
    held = ctx.state.delayed_start.get(app.ha_id)
    built = build_start(app, held, ctx.wall, ctx.lang, ctx.label, ctx.pending_program)
    if isinstance(built, Refusal):
        return built
    blocked = _held_back(ctx, "start")
    if blocked is not None:
        return blocked
    request, _ = built  # its note is logged when the request is rebuilt for sending
    return Intent(
        app.ha_id,
        "start",
        ctx.pending_program or app.selected_program or "",
        None,
        request,
        catalog.word("action_start", ctx.lang),
        limit="start",
        verify="status",
    )


def _stop(ctx: Context, app: model.Appliance) -> Intent | Refusal:
    lang = ctx.lang
    describe = catalog.word("action_stop", lang)
    running = _running(app)
    if not running and not ctx.sent_start:
        if ctx.pending_start:
            # Nothing runs yet: the Stop only cancels the Start waiting to be sent (spec 7.6).
            return Intent(app.ha_id, "stop", "", None, None, describe)
        return Refusal(message("nothing_to_stop", lang, a=ctx.label))
    # A sent Start may still take: this Stop cancels any Start still waiting and follows the
    # sent one, so a program that starts is stopped as the user asked.
    blocked = _held_back(ctx, "stop")
    if blocked is not None:
        return blocked
    return Intent(
        app.ha_id,
        "stop",
        "",
        None,
        stop_request(app.ha_id),
        describe,
        limit="stop",
        verify="status",
        follows_start=not running,
    )


def _delay(ctx: Context, app: model.Appliance, command: str, level: object) -> Hold | Refusal:
    lang = ctx.lang
    if command not in ("Set Level", "Off"):
        return _unsupported(command, lang)
    number = 0.0 if command == "Off" else as_number(level)
    minutes: int | None = None
    if number != 0:
        times = planner.delay_times()
        index = _level_index(level, len(times))
        if index is None:
            return Refusal(message("level_unavailable", lang))
        minutes = times[index]
    shown = planner.clock_text(minutes) if minutes is not None else "-"
    if app.operation_state() != "DelayedStart":
        if minutes is None:
            return Hold(None, message("delay_cleared", lang))
        return Hold(minutes, message("delay_held", lang, time=shown))
    if minutes is None:
        return Refusal(message("delay_now_running", lang))
    if not ctx.authorized:
        return Refusal(message("not_authorized", lang))
    if not app.connected:
        return Refusal(message("offline", lang, a=ctx.label))
    program = app.active_program
    # The option the waiting program reports wins; its details are the fallback.
    key = planner.pick_delay_key(app.options)
    if key is None and program is not None:
        key = delay_option_of(app, program)
    if key is None:
        # Not no_delay_option: its '-' is refused while the program waits.
        return Refusal(message("delay_unknown", lang))
    detail = program_option(app, program, key) if program is not None else None
    delay = delay_seconds(minutes, planner.constraints_of(detail), ctx.wall, lang)
    if isinstance(delay, Refusal):
        return delay
    blocked = _held_back(ctx)
    if blocked is not None:
        return blocked
    unit_text = str(detail.get("unit") or "") if detail is not None else ""
    name = catalog.name_for(app.names, key, lang)
    # The held time (Hold.minutes) is stored only on confirmation; until then the unit shows
    # the move as a request, and returns to the held time if the write does not succeed.
    level_value = planner.delay_level(minutes)
    intent = Intent(
        app.ha_id,
        "delay",
        key,
        delay.seconds,
        option_request(app.ha_id, key, delay.seconds, unit_text, running=True),
        f"{name}: {shown}",
        overlay=(planner.UNIT_DELAYED_START, level_value, str(level_value)),
        coalesce=f"option:{key}",
        note=delay.note,
    )
    return Hold(minutes, message("delay_moved", lang, time=shown), intent)
