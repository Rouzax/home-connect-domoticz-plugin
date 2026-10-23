"""Home Connect error responses to categories. Pure.

Two different shapes exist and are never guessed between (spec 3.5, 8): the appliance API returns
{"error": {"key", "description"}}, the OAuth endpoints return {"error": "<code>",
"error_description"}. The caller picks the parser by endpoint.

`description` on ApiError and OAuthError is untrusted text from Home Connect. Callers escape and
bound it before showing it, and OAuth descriptions are never logged (spec 7.11).
"""

import json
from dataclasses import dataclass

NETWORK = "network"
AUTH = "auth"
FORBIDDEN = "forbidden"
OFFLINE = "offline"
STATE = "state"
REFUSED_USER = "refused_user"
REFUSED_VALIDATION = "refused_validation"
LIMITED = "limited"
SERVER = "server"
UNKNOWN = "unknown"

_STATE_KEYS = {
    "SDK.Error.NoProgramSelected",
    "SDK.Error.NoProgramActive",
    "SDK.Error.WrongOperationState",
    "SDK.Error.ActiveProgramSet",
    "SDK.Error.ActiveProgramNotSet",
    "SDK.Error.SelectedProgramNotSet",
    "SDK.Error.ProgramNotAvailable",
    "SDK.Error.ProgramNotWritable",
    "SDK.Error.NotInSimulator",
}
_OFFLINE_KEYS = {"SDK.Error.HomeAppliance.Connection.Initialization.Failed"}
_USER_KEYS = {
    "BSH.Common.Error.RemoteControlNotActive",
    "BSH.Common.Error.RemoteStartNotActive",
    "BSH.Common.Error.LockedByLocalControl",
    # The appliance refused without a reason; the same write often succeeds seconds later.
    "BSH.Common.Error.400.BadRequest",
    "SDK.Error.WrongDoorState",
    "SDK.Error.Cooking.Oven.Status.FrontPanelOpen",
    "SDK.Error.Cooking.Oven.Status.MeatprobePlugged",
    "SDK.Error.BatteryLevelTooLow",
    "SDK.Error.WrongProgramState",
    "SDK.Error.ConsumerProducts.CleaningRobot.Status.Lifted",
    "SDK.Error.ConsumerProducts.CleaningRobot.Status.DustBoxNotInserted",
    "SDK.Error.ConsumerProducts.CleaningRobot.Status.AlreadyAtHome",
}
_BY_STATUS = {
    400: REFUSED_VALIDATION,
    401: AUTH,
    403: FORBIDDEN,
    404: STATE,
    405: REFUSED_VALIDATION,
    406: REFUSED_VALIDATION,
    408: SERVER,
    409: REFUSED_VALIDATION,
    415: REFUSED_VALIDATION,
    429: LIMITED,
}

_USER_MESSAGES = {
    "BSH.Common.Error.RemoteControlNotActive": {
        "en": "Remote control is not enabled on {a}; enable it once on the appliance",
        "nl": "Bediening op afstand staat niet aan op {a}; zet het eenmalig aan op het apparaat",
    },
    "BSH.Common.Error.RemoteStartNotActive": {
        "en": "Remote start is not enabled on {a}; press remote start on the appliance",
        "nl": (
            "Op afstand starten staat niet aan op {a}; druk op het apparaat op 'Op afstand starten'"
        ),
    },
    "BSH.Common.Error.LockedByLocalControl": {
        "en": "{a} is being operated on its panel; try again in a few seconds",
        "nl": (
            "{a} wordt op het bedieningspaneel bediend; probeer het over enkele seconden opnieuw"
        ),
    },
    "BSH.Common.Error.400.BadRequest": {
        "en": "{a} refused it without a reason; check that it is switched on, or try again in a"
        " few seconds",
        "nl": "{a} weigerde het zonder reden; controleer of het aan staat, of probeer het over"
        " enkele seconden opnieuw",
    },
    "SDK.Error.WrongDoorState": {
        "en": "The door of {a} is not closed",
        "nl": "De deur van {a} is niet dicht",
    },
    "SDK.Error.Cooking.Oven.Status.FrontPanelOpen": {
        "en": "The front panel of {a} is open",
        "nl": "Het frontpaneel van {a} is open",
    },
    "SDK.Error.Cooking.Oven.Status.MeatprobePlugged": {
        "en": "The meat probe is plugged into {a}; remove it or use a probe program",
        "nl": "De kerntemperatuurmeter zit in {a}; haal hem eruit of kies een programma ermee",
    },
    "SDK.Error.WrongProgramState": {
        "en": "The program on {a} cannot be stopped at this point",
        "nl": "Het programma op {a} kan nu niet worden gestopt",
    },
    "SDK.Error.BatteryLevelTooLow": {
        "en": "The battery of {a} is too low; let it charge first",
        "nl": "De accu van {a} is te leeg; laat hem eerst opladen",
    },
    "SDK.Error.ConsumerProducts.CleaningRobot.Status.Lifted": {
        "en": "{a} has been lifted; put it back on the floor",
        "nl": "{a} is opgetild; zet hem terug op de vloer",
    },
    "SDK.Error.ConsumerProducts.CleaningRobot.Status.DustBoxNotInserted": {
        "en": "The dust box of {a} is not inserted",
        "nl": "Het stofreservoir van {a} zit er niet in",
    },
    "SDK.Error.ConsumerProducts.CleaningRobot.Status.AlreadyAtHome": {
        "en": "{a} is already at its charging station",
        "nl": "{a} staat al in het laadstation",
    },
}


@dataclass(frozen=True)
class ApiError:
    status: int
    key: str | None
    description: str
    category: str


@dataclass(frozen=True)
class OAuthError:
    status: int
    code: str
    description: str


def parse_json(body: bytes | None) -> object:
    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        # UnicodeDecodeError is a ValueError, so one clause covers undecodable and non-JSON bodies.
        # json.loads recurses per nesting level, so a hostile deep body raises RecursionError;
        # it is no more usable than a malformed one.
        return None


def _category(status: int, key: str | None) -> str:
    if key in _OFFLINE_KEYS:
        return OFFLINE
    if key in _STATE_KEYS:
        return STATE
    if key in _USER_KEYS:
        return REFUSED_USER
    if key and (key.startswith("SDK.Error.Invalid") or key.startswith("SDK.Error.Unsupported")):
        return REFUSED_VALIDATION
    if key == "SDK.Error.MissingOptionValue":
        return REFUSED_VALIDATION
    if key == "invalid_token":
        return AUTH
    if key == "insufficient_scope":
        return FORBIDDEN
    if status >= 500:
        return SERVER
    return _BY_STATUS.get(status, UNKNOWN)


def parse_api_error(status: int, body: bytes | None) -> ApiError:
    data = parse_json(body)
    error = data.get("error") if isinstance(data, dict) else None
    # A missing key is "" (no key was sent); a present but non-string key such as null is None,
    # so it can never be mistaken for the text "None".
    key: str | None = ""
    description = ""
    if isinstance(error, dict):
        raw_key = error.get("key", "")
        key = raw_key if raw_key is None or isinstance(raw_key, str) else None
        raw_description = error.get("description", "")
        description = raw_description if isinstance(raw_description, str) else ""
    return ApiError(status, key, description, _category(status, key))


def parse_oauth_error(status: int, body: bytes | None) -> OAuthError:
    data = parse_json(body)
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return OAuthError(status, data["error"], str(data.get("error_description", "")))
    return OAuthError(status, "unknown", "")


def counts_for_brake(category: str) -> bool:
    """Home Connect counts every error response toward its 10-in-10-minutes block, state
    answers included; a request that never reached it (NETWORK) cannot count."""
    return category != NETWORK


def user_message(key: str, appliance: str, language: str) -> str | None:
    messages = _USER_MESSAGES.get(key)
    if messages is None:
        return None
    return messages.get(language, messages["en"]).format(a=appliance)
