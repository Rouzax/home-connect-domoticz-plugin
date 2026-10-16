"""Presentation knowledge per Home Connect key (spec 4.1). Pure.

This module only decides how something is SHOWN. Whether a key gets a device at all, and whether
it is writable, comes from what the appliance reports (spec 1, discovery-driven).
"""

import re
from dataclasses import dataclass

GREY, GREEN, YELLOW, ORANGE, RED = 0, 1, 2, 3, 4  # Domoticz Alert levels

OPERATION_STATE = "BSH.Common.Status.OperationState"
POWER_STATE = "BSH.Common.Setting.PowerState"
DOOR_STATE = "BSH.Common.Status.DoorState"
PROGRESS = "BSH.Common.Option.ProgramProgress"
REMAINING = "BSH.Common.Option.RemainingProgramTime"
ELAPSED = "BSH.Common.Option.ElapsedProgramTime"
ESTIMATED = "BSH.Common.Option.RemainingProgramTimeIsEstimated"
REMOTE_ACTIVE = "BSH.Common.Status.RemoteControlActive"
REMOTE_START = "BSH.Common.Status.RemoteControlStartAllowed"
LOCAL_CONTROL = "BSH.Common.Status.LocalControlActive"
START_IN = "BSH.Common.Option.StartInRelative"
FINISH_IN = "BSH.Common.Option.FinishInRelative"
FIXED_KEYS = frozenset(
    {
        OPERATION_STATE,
        POWER_STATE,
        DOOR_STATE,
        PROGRESS,
        REMAINING,
        ELAPSED,
        ESTIMATED,
        REMOTE_ACTIVE,
        REMOTE_START,
        LOCAL_CONTROL,
    }
)

STATE_COLOUR = {
    "Inactive": GREY,
    "Ready": GREY,
    "DelayedStart": GREEN,
    "Run": GREEN,
    "Pause": YELLOW,
    "Finished": YELLOW,
    "Aborting": YELLOW,
    "ActionRequired": ORANGE,
    "Error": RED,
}
EVENT_LEVEL_COLOUR = {"info": GREY, "hint": YELLOW, "warning": ORANGE, "alert": RED}

# Domoticz built-in icon ids: zero-based line numbers of www/switch_icons.txt. The plugin API's
# Image= sets the CustomImage column (hardware/plugins/PythonObjectEx.cpp), and domoticz_api
# applies it only when a unit is created, so an icon the user picks later is kept (spec 6.4).
IMAGE_FAN = 7
IMAGE_WATER = 11
IMAGE_HEATING = 15
IMAGE_COOLING = 16
IMAGE_CLOCK = 21
IMAGE_COFFEE = 24
IMAGE_WASHING_MACHINE = 28

_APPLIANCE_IMAGES = {
    "Washer": IMAGE_WASHING_MACHINE,
    "Dryer": IMAGE_WASHING_MACHINE,
    "WasherDryer": IMAGE_WASHING_MACHINE,
    "Oven": IMAGE_HEATING,
    "CookProcessor": IMAGE_HEATING,
    "Microwave": IMAGE_HEATING,
    "WarmingDrawer": IMAGE_HEATING,
    "Hob": IMAGE_HEATING,
    "Cooktop": IMAGE_HEATING,
    "FridgeFreezer": IMAGE_COOLING,
    "Refrigerator": IMAGE_COOLING,
    "Freezer": IMAGE_COOLING,
    "WineCooler": IMAGE_COOLING,
    "CoffeeMaker": IMAGE_COFFEE,
    "Hood": IMAGE_FAN,
    "Dishwasher": IMAGE_WATER,
}

_STATES = {
    "Inactive": ("Inactive", "Niet actief"),
    "Ready": ("Ready", "Gereed"),
    "DelayedStart": ("Delayed start", "Uitgestelde start"),
    "Run": ("Running", "Bezig"),
    "Pause": ("Paused", "Gepauzeerd"),
    "ActionRequired": ("Action required", "Actie vereist"),
    "Finished": ("Finished", "Klaar"),
    "Error": ("Error", "Fout"),
    "Aborting": ("Aborting", "Wordt afgebroken"),
}
_WORDS = {
    "state": ("State", "Status"),
    "power": ("Power", "Aan/uit"),
    "door": ("Door", "Deur"),
    "progress": ("Progress", "Voortgang"),
    "remaining": ("Remaining time", "Resterende tijd"),
    "program": ("Program", "Programma"),
    "remote": ("Remote control", "Bediening op afstand"),
    "offline": ("Offline", "Offline"),
    "last_known": ("last known", "laatst bekend"),
    "locked": ("Locked", "Vergrendeld"),
    "selected": ("selected", "gekozen"),
    "ready_at": ("ready at", "klaar om"),
    "about": ("about", "ongeveer"),
    "not_available": ("Not available", "Niet beschikbaar"),
    "no_notification": ("No notification", "Geen melding"),
    "status_code": ("Status code", "Statuscode"),
    "unknown": ("Unknown", "Onbekend"),
    "local": ("Local operation", "Lokale bediening"),
    "start_allowed": ("Remote start allowed", "Op afstand starten toegestaan"),
    "remote_on": ("Remote control on", "Bediening op afstand aan"),
    "remote_off": ("Remote control off", "Bediening op afstand uit"),
    "yes": ("Yes", "Ja"),
    "no": ("No", "Nee"),
    "account": ("Home Connect", "Home Connect"),
    "account_actions": ("Home Connect actions", "Home Connect acties"),
    "calls": ("Home Connect API calls 24h", "Home Connect API-aanroepen 24u"),
    "reauthorize": ("Re-authorize", "Opnieuw autoriseren"),
    "finished": ("finished", "klaar"),
    "login_link": ("Log in to Home Connect", "Inloggen bij Home Connect"),
    "refresh": ("Refresh appliances", "Apparaten verversen"),
}


@dataclass(frozen=True)
class KeyInfo:
    kind: str
    names: tuple[str, str]
    resolution: float = 0.0
    deadband: float = 0.0


_SPECIAL = {
    # Float noise while merely on (22.999999999999996), and the docs call it inaccurate: whole
    # degrees with a half-degree deadband (spec 6.4).
    "Cooking.Oven.Status.CurrentCavityTemperature": KeyInfo(
        "temperature", ("Cavity temperature", "Temperatuur ovenruimte"), 1.0, 0.5
    ),
}

# EN/NL names for documented keys. They win over the names Home Connect sends, which are often
# long or awkward ("Basic dosing volume: detergent"); Home Connect's name is used only for a key
# not listed here. A key listed here (or in _SPECIAL or FIXED_KEYS) is "known": its device is
# shown on creation, while a device for any other key is created hidden (spec 6.4).
_NAMES = {
    "BSH.Common.Option.Duration": ("Duration", "Duur"),
    "BSH.Common.Option.EnergyForecast": ("Energy forecast", "Energieprognose"),
    "BSH.Common.Option.WaterForecast": ("Water forecast", "Waterprognose"),
    "BSH.Common.Option.EstimatedTotalProgramTime": (
        "Estimated program time",
        "Geschatte programmaduur",
    ),
    "BSH.Common.Setting.AlarmClock": ("Timer", "Timer"),
    "BSH.Common.Setting.AmbientLightEnabled": ("Ambient light", "Sfeerverlichting"),
    "BSH.Common.Setting.AmbientLightBrightness": ("Ambient brightness", "Helderheid sfeerlicht"),
    "BSH.Common.Setting.AmbientLightColor": ("Ambient colour", "Kleur sfeerlicht"),
    "BSH.Common.Setting.ChildLock": ("Child lock", "Kinderslot"),
    "BSH.Common.Status.InteriorIlluminationActive": ("Interior light", "Binnenverlichting"),
    "BSH.Common.Event.AlarmClockElapsed": ("Alarm clock elapsed", "Kookwekker afgelopen"),
    "BSH.Common.Event.ProgramAborted": ("Program aborted", "Programma afgebroken"),
    "BSH.Common.Event.ProgramFinished": ("Program finished", "Programma klaar"),
    "ConsumerProducts.CoffeeMaker.Option.BeanAmount": ("Bean amount", "Hoeveelheid bonen"),
    "ConsumerProducts.CoffeeMaker.Option.CoffeeTemperature": (
        "Coffee temperature",
        "Koffietemperatuur",
    ),
    "ConsumerProducts.CoffeeMaker.Option.FillQuantity": ("Fill quantity", "Vulhoeveelheid"),
    "Cooking.Oven.Option.FastPreHeat": ("Fast preheat", "Snel voorverwarmen"),
    "Cooking.Oven.Option.SetpointTemperature": ("Set temperature", "Ingestelde temperatuur"),
    "Cooking.Common.Setting.Lighting": ("Light", "Verlichting"),
    "Cooking.Common.Setting.LightingBrightness": ("Light brightness", "Helderheid verlichting"),
    "Cooking.Oven.Setting.SabbathMode": ("Sabbath mode", "Sabbatmodus"),
    "Cooking.Oven.Event.PreheatFinished": ("Preheat finished", "Voorverwarmen klaar"),
    "Cooking.Oven.Event.RegularPreheatFinished": (
        "Regular preheat finished",
        "Normaal voorverwarmen klaar",
    ),
    "Dishcare.Dishwasher.Option.BrillianceDry": ("Brilliance dry", "Brilliance dry"),
    "Dishcare.Dishwasher.Option.HygienePlus": ("Hygiene plus", "Hygiëne plus"),
    "Dishcare.Dishwasher.Option.IntensivZone": ("Intensive zone", "Intensieve zone"),
    "Dishcare.Dishwasher.Option.SilenceOnDemand": ("Silence on demand", "Silence on demand"),
    "Dishcare.Dishwasher.Option.VarioSpeedPlus": ("VarioSpeed plus", "VarioSpeed plus"),
    "Dishcare.Dishwasher.Event.RinseAidLack": ("Rinse aid empty", "Glansspoelmiddel op"),
    "Dishcare.Dishwasher.Event.RinseAidNearlyEmpty": (
        "Rinse aid nearly empty",
        "Glansspoelmiddel bijna op",
    ),
    "Dishcare.Dishwasher.Event.SaltLack": ("Salt empty", "Zout op"),
    "Dishcare.Dishwasher.Event.SaltNearlyEmpty": ("Salt nearly empty", "Zout bijna op"),
    "Dishcare.Dishwasher.Event.MachineCareReminder": ("Machine care", "Machineonderhoud"),
    "LaundryCare.Common.Option.ProcessPhase": ("Process phase", "Programmafase"),
    "LaundryCare.Common.Option.SilentMode": ("Silent mode", "Stille modus"),
    "LaundryCare.Common.Option.SpeedPerfect": ("SpeedPerfect", "SpeedPerfect"),
    "LaundryCare.Dryer.Option.DryingTarget": ("Drying target", "Droogdoel"),
    "LaundryCare.Dryer.Option.Gentle": ("Gentle", "Zacht drogen"),
    "LaundryCare.Dryer.Option.HalfLoad": ("Half load", "Halve lading"),
    "LaundryCare.Dryer.Option.WrinkleGuard": ("Wrinkle guard", "Kreukbescherming"),
    "LaundryCare.Dryer.Event.DryingProcessFinished": ("Drying finished", "Drogen klaar"),
    "LaundryCare.Washer.Option.IDos1Active": ("i-DOS 1", "i-DOS 1"),
    "LaundryCare.Washer.Option.IDos2Active": ("i-DOS 2", "i-DOS 2"),
    "LaundryCare.Washer.Option.LessIroning": ("Less ironing", "Minder strijken"),
    "LaundryCare.Washer.Option.MiniLoad": ("Mini load", "Kleine lading"),
    "LaundryCare.Washer.Option.Prewash": ("Prewash", "Voorwas"),
    "LaundryCare.Washer.Option.RinseHold": ("Rinse hold", "Spoelstop"),
    "LaundryCare.Washer.Option.RinsePlus": ("Rinse plus", "Extra spoelen"),
    "LaundryCare.Washer.Option.Soak": ("Soak", "Weken"),
    "LaundryCare.Washer.Option.SpinSpeed": ("Spin speed", "Centrifugetoerental"),
    "LaundryCare.Washer.Option.Stains": ("Stains", "Vlekken"),
    "LaundryCare.Washer.Option.Temperature": ("Temperature", "Temperatuur"),
    "LaundryCare.Washer.Option.WaterPlus": ("Water plus", "Extra water"),
    "LaundryCare.Washer.Setting.IDos1BaseLevel": ("i-DOS 1 base dose", "i-DOS 1 basisdosering"),
    "LaundryCare.Washer.Setting.IDos2BaseLevel": ("i-DOS 2 base dose", "i-DOS 2 basisdosering"),
    "LaundryCare.Washer.Event.IDos1FillLevelPoor": ("i-DOS 1 nearly empty", "i-DOS 1 bijna leeg"),
    "LaundryCare.Washer.Event.IDos2FillLevelPoor": ("i-DOS 2 nearly empty", "i-DOS 2 bijna leeg"),
    "Refrigeration.FridgeFreezer.Setting.SetpointTemperatureFreezer": (
        "Freezer temperature",
        "Temperatuur vriezer",
    ),
    "Refrigeration.FridgeFreezer.Setting.SetpointTemperatureRefrigerator": (
        "Fridge temperature",
        "Temperatuur koelkast",
    ),
    "Refrigeration.FridgeFreezer.Setting.SuperModeFreezer": ("Super freezing", "Supervriezen"),
    "Refrigeration.FridgeFreezer.Setting.SuperModeRefrigerator": ("Super cooling", "Superkoelen"),
    "Refrigeration.Common.Status.Door.BottleCooler": ("Bottle cooler door", "Deur flessenkoeler"),
    "Refrigeration.Common.Status.Door.Chiller": ("Chiller door", "Deur koelvak"),
    "Refrigeration.Common.Status.Door.ChillerCommon": (
        "Common chiller door",
        "Deur gemeenschappelijk koelvak",
    ),
    "Refrigeration.Common.Status.Door.ChillerLeft": ("Left chiller door", "Deur koelvak links"),
    "Refrigeration.Common.Status.Door.ChillerRight": ("Right chiller door", "Deur koelvak rechts"),
    "Refrigeration.Common.Status.Door.FlexCompartment": ("Flex compartment door", "Deur flexvak"),
    "Refrigeration.Common.Status.Door.Freezer": ("Freezer door", "Deur vriezer"),
    "Refrigeration.Common.Status.Door.Refrigerator": ("Fridge door", "Deur koelkast"),
    "Refrigeration.Common.Status.Door.Refrigerator2": ("Fridge door 2", "Deur koelkast 2"),
    "Refrigeration.Common.Status.Door.Refrigerator3": ("Fridge door 3", "Deur koelkast 3"),
    "Refrigeration.Common.Status.Door.WineCompartment": ("Wine compartment door", "Deur wijnvak"),
    "Refrigeration.FridgeFreezer.Event.DoorAlarmFreezer": (
        "Freezer door alarm",
        "Deuralarm vriezer",
    ),
    "Refrigeration.FridgeFreezer.Event.DoorAlarmRefrigerator": (
        "Fridge door alarm",
        "Deuralarm koelkast",
    ),
    "Refrigeration.FridgeFreezer.Event.TemperatureAlarmFreezer": (
        "Freezer temperature alarm",
        "Temperatuuralarm vriezer",
    ),
}


def _pick(pair: tuple[str, str], lang: str) -> str:
    return pair[1] if lang == "nl" else pair[0]


def word(name: str, lang: str) -> str:
    return _pick(_WORDS[name], lang)


def state_name(state: str, lang: str) -> str:
    return _pick(_STATES[state], lang) if state in _STATES else humanize(state)


def humanize(key: str) -> str:
    last = key.rsplit(".", 1)[-1]
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", last).split()
    return " ".join(words).capitalize() if words else key


def special(key: str) -> KeyInfo | None:
    return _SPECIAL.get(key)


def image_for(kind: str, appliance_type: str) -> int:
    """The built-in icon for a device of this kind, or 0 for the Domoticz default.

    Only switch-type devices and the General/Custom sensor draw CustomImage (WebServer.cpp falls
    back to the type's own image only `if (!CustomImage)` in those branches); Alert, Text, Set
    Point, Percentage and Temperature always draw their own icon, so they get none. A door contact
    keeps its open/closed rendering.
    """
    if kind == "minutes":
        return IMAGE_CLOCK
    if kind in ("switch", "selector", "push"):
        return _APPLIANCE_IMAGES.get(appliance_type, 0)
    return 0


def is_favourite(key: str) -> bool:
    """Favourite triggers (hob, hood) are numbered per appliance, so they match by pattern."""
    return ".Event.Favorite." in key


def is_compartment_door(key: str) -> bool:
    """Per-compartment fridge door contacts (spec 6.2). Status keys only: the
    Refrigeration.Common.Setting.Door.* keys configure the door assistant, they are not doors."""
    return key.startswith("Refrigeration.") and ".Status.Door." in key


def known(key: str) -> bool:
    return (
        key in FIXED_KEYS
        or key in _SPECIAL
        or key in _NAMES
        or is_favourite(key)
        or is_compartment_door(key)
    )


def catalog_name(key: str, lang: str) -> str | None:
    """The catalog's own name for `key`, or None when the catalog does not name it."""
    info = _SPECIAL.get(key)
    if info:
        return _pick(info.names, lang)
    return _pick(_NAMES[key], lang) if key in _NAMES else None


def name_for(app_names: dict[str, str], key: str, lang: str) -> str:
    """The catalog's name, else the one Home Connect reported, else the key humanized."""
    return catalog_name(key, lang) or app_names.get(key) or humanize(key)
