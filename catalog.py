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
ACKNOWLEDGE = "BSH.Common.Command.AcknowledgeEvent"
PAUSE = "BSH.Common.Command.PauseProgram"
RESUME = "BSH.Common.Command.ResumeProgram"
OPEN_DOOR = "BSH.Common.Command.OpenDoor"
PARTLY_OPEN_DOOR = "BSH.Common.Command.PartlyOpenDoor"
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
    "reauthorize": ("Sign in again", "Opnieuw aanmelden"),
    "finished": ("finished", "klaar"),
    "login_link": ("Sign in to Home Connect", "Aanmelden bij Home Connect"),
    "refresh": ("Refresh appliances", "Apparaten verversen"),
    "program_select": ("Program choice", "Programmakeuze"),
    "actions": ("Actions", "Acties"),
    "acknowledge": ("Acknowledge", "Bevestigen"),
    "start_at_name": ("Start at", "Start om"),
    "ready_at_name": ("Ready at", "Klaar om"),
    "action_start": ("Start", "Start"),
    "action_stop": ("Stop", "Stop"),
    "action_pause": ("Pause", "Pauze"),
    "action_resume": ("Resume", "Hervatten"),
    "action_open_door": ("Open door", "Deur openen"),
    "action_partly_open_door": ("Partly open door", "Deur op een kier"),
}

# Device descriptions, shown in the device's edit dialog and set at creation only, so a user's own
# text survives. {name} fields are filled from _WORDS, so a description names a card exactly as
# the card is called. Keep each to one or two short sentences: the dialog is small.
_DESCRIPTIONS = {
    "state": (
        'What the appliance is doing now, with the running program; "{last_known}" means no '
        "update has arrived for a while.",
        'Wat het apparaat nu doet, met het lopende programma; "{last_known}" betekent dat er al '
        "een tijd geen update is binnengekomen.",
    ),
    "power": (
        "Switches the appliance on or off; some appliances can be switched on here but not off.",
        "Zet het apparaat aan of uit; sommige apparaten kun je hier wel aanzetten, maar niet "
        "uitzetten.",
    ),
    "door": (
        "Shows whether the door is open. A locked door counts as closed, and the {state} card "
        'then shows "{locked}".',
        "Toont of de deur open is. Een vergrendelde deur telt als dicht, en de kaart {state} "
        'toont dan "{locked}".',
    ),
    "progress": (
        "How far the running program is, from 0 to 100%.",
        "Hoe ver het lopende programma is, van 0 tot 100%.",
    ),
    "remaining": (
        "Minutes until the running program ends; for an oven program without a set time, the "
        "minutes since it started.",
        "Minuten tot het lopende programma klaar is; bij een ovenprogramma zonder ingestelde tijd "
        "de minuten sinds de start.",
    ),
    "program": (
        'The running program and when it will be ready. "({selected})" means chosen on the '
        "appliance or in {program_select}, but not started yet.",
        'Het lopende programma en wanneer het klaar is. "({selected})" betekent: gekozen op het '
        "apparaat of in {program_select}, maar nog niet gestart.",
    ),
    "remote": (
        "Whether the appliance accepts a start from Domoticz; switch remote start on at the "
        "appliance itself.",
        "Of het apparaat een start vanuit Domoticz accepteert; zet starten op afstand aan op het "
        "apparaat zelf.",
    ),
    "status_code": (
        "The same as {state}, as a fixed level for scripts: {levels}.",
        "Hetzelfde als {state}, als vast niveau voor scripts: {levels}.",
    ),
    "program_select": (
        "Choose a program; choosing does not start it, Start in {actions} does.",
        "Kies een programma; kiezen start het nog niet, dat doe je met Start in {actions}.",
    ),
    "actions": (
        'Start, stop, pause or resume the program; returns to "-" after each command.',
        'Start, stop, pauzeer of hervat het programma; springt na elke opdracht terug naar "-".',
    ),
    "actions_doors": (
        "Start, stop, pause or resume the program, or open the oven door; returns to "
        '"-" after each command.',
        "Start, stop, pauzeer of hervat het programma, of open de ovendeur; springt na elke "
        'opdracht terug naar "-".',
    ),
    "acknowledge": (
        "Confirms a message on the appliance, such as a finished program, like the button on the "
        "appliance does.",
        "Bevestigt een melding op het apparaat, zoals een afgerond programma, net als de knop op "
        "het apparaat.",
    ),
    "start_at": (
        'Pick a start time, then choose Start in {actions}; with "-" Start begins straight '
        "away. A chosen time applies to one run only.",
        'Kies een starttijd en daarna Start in {actions}; bij "-" start het programma meteen. '
        "Een gekozen tijd geldt voor één keer.",
    ),
    "ready_at": (
        'Pick a finish time, then choose Start in {actions}; with "-" Start begins straight '
        "away. A chosen time applies to one run only.",
        'Kies een eindtijd en daarna Start in {actions}; bij "-" start het programma meteen. '
        "Een gekozen tijd geldt voor één keer.",
    ),
    "option": (
        "An option for the selected program; set it before Start.",
        "Een optie voor het gekozen programma; stel die in vóór Start.",
    ),
    "option_reading": (
        "Reported by the appliance for the selected or running program.",
        "Gemeld door het apparaat voor het gekozen of lopende programma.",
    ),
    "setting": (
        "An appliance setting, the same as on the appliance itself.",
        "Een instelling van het apparaat, dezelfde als op het apparaat zelf.",
    ),
    "setting_reading": (
        "An appliance setting, shown as the appliance reports it.",
        "Een instelling van het apparaat, zoals het apparaat die meldt.",
    ),
    "reading": ("A value reported by the appliance.", "Een waarde die het apparaat meldt."),
    "total": (
        "Lifetime total reported by the appliance at the end of each program. Domoticz shows the "
        "use per day, month and year in its log.",
        "Totaal sinds ingebruikname, door het apparaat gemeld aan het eind van elk programma. "
        "Domoticz toont het gebruik per dag, maand en jaar in het logboek.",
    ),
    "event": (
        'A message from the appliance; it reads "{no_notification}" when there is none.',
        'Een melding van het apparaat; er staat "{no_notification}" als er geen is.',
    ),
    "favourite": (
        "Turns on briefly when the favourite button on the appliance is pressed; use it to "
        "trigger a scene or script.",
        "Gaat kort aan als de favorietknop op het apparaat wordt ingedrukt; gebruik het om een "
        "scène of script te starten.",
    ),
    "control_off": (
        "Works only while Allow control is on in the hardware settings.",
        "Werkt alleen als Allow control aan staat in de hardware-instellingen.",
    ),
    "account": (
        "Sign-in status and plugin notes. Shown while you need to sign in, with the link and "
        "code; once signed in it is hidden, find it under Setup, Devices.",
        "Aanmeldstatus en meldingen van de plugin. Zichtbaar zolang je je moet aanmelden, met de "
        "link en code; daarna verborgen, je vindt het dan onder Instellingen, Apparaten.",
    ),
    "account_actions": (
        '"{reauthorize}" shows a new sign-in link and code; "{refresh}" reloads the appliances '
        'and their programs. Returns to "-" afterwards.',
        '"{reauthorize}" toont een nieuwe aanmeldlink en code; "{refresh}" laadt de apparaten '
        'en hun programma\'s opnieuw. Springt daarna terug naar "-".',
    ),
    "calls": (
        "Requests to Home Connect in the last 24 hours; Home Connect allows 1000 a day.",
        "Aanroepen naar Home Connect in de afgelopen 24 uur; Home Connect staat er 1000 per dag "
        "toe.",
    ),
}

# Plugin notes on the account Text device, (English, Dutch).
# Each note becomes one bullet on the account card, a small tile: keep them short.
_NOTES = {
    "update_subject": (
        "Home Connect plugin {version} is available",
        "Home Connect-plugin {version} is beschikbaar",
    ),
    "update_body": (
        "You run {current}. What changed and how to update: {url}",
        "Je gebruikt {current}. Wat er veranderd is en hoe je bijwerkt: {url}",
    ),
    "workers_down": (
        "Workers stopped; retrying hourly",
        "Achtergrondprocessen gestopt; elk uur opnieuw",
    ),
    "background_exhausted": (
        "Background budget used up; events continue",
        "Achtergrondbudget op; events gaan door",
    ),
    "total_exhausted": (
        "API budget used up; updates resume later",
        "API-budget op; updates volgen later",
    ),
    "auth_exhausted": ("Sign-in budget used up; try later", "Aanmeldbudget op; probeer later"),
    "denied": ("Sign-in denied", "Aanmelding geweigerd"),
    "flow_failed": ("Failed ({reason})", "Mislukt ({reason})"),
    "client_rejected": (
        "Client ID or Secret refused; check settings",
        "Client ID of Secret geweigerd; controleer instellingen",
    ),
    "client_not_device_flow": (
        "Client ID is not a Device Flow app; register one at developer.home-connect.com",
        "Client ID is geen Device Flow-app; registreer er een op developer.home-connect.com",
    ),
    "refresh_refused": ("Renewal refused ({reason})", "Verlenging geweigerd ({reason})"),
    "start_failed": (
        "Start failed: {reason}; check the Client ID",
        "Start mislukt: {reason}; controleer de Client ID",
    ),
}

# Sign-in states as the log names them (spec 7.11 keeps the log in English).
_SIGN_IN_STATES = {
    "AUTHORIZED": "signed in",
    "UNAUTHORIZED": "not signed in yet",
    "AWAITING_USER": "waiting for you to sign in",
    "EXPIRED_CODE": "sign-in code expired",
    "NEEDS_REAUTH": "sign-in lost",
    "NEEDS_USER_ACTION": "action needed in your Home Connect account",
    "SIGNED_OUT": "signed out",
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


@dataclass(frozen=True)
class MeterInfo:
    """A lifetime total shown as a Domoticz meter: `quantity` (en, nl) labels the graph,
    `units` is the unit written, `reported` the unit Home Connect reports the total in, and
    `divisor` turns that into `units`."""

    kind: str
    quantity: tuple[str, str]
    units: str
    reported: str
    divisor: int


# Totals only grow and arrive at program ends; Domoticz derives day and month usage from the
# difference of successive totals, so the absolute value is written (spec 6.2).
_METERS = {
    "BSH.Common.Status.Program.All.Energy.Consumed": MeterInfo(
        "energy_total", ("Energy", "Energie"), "Wh", "Wh", 1
    ),
    # Litres: a water-type meter needs a per-device divider a plugin cannot set.
    "BSH.Common.Status.Program.All.Water.Consumed": MeterInfo(
        "counter_total", ("Water", "Water"), "L", "ml", 1000
    ),
    "LaundryCare.Washer.Status.Detergent.All.Consumed": MeterInfo(
        "counter_total", ("Detergent", "Wasmiddel"), "ml", "ml", 1
    ),
    "LaundryCare.Washer.Status.Softener.All.Consumed": MeterInfo(
        "counter_total", ("Softener", "Wasverzachter"), "ml", "ml", 1
    ),
}

# EN/NL names for documented keys. They win over the names Home Connect sends, which are often
# long or awkward ("Basic dosing volume: detergent"); Home Connect's name is used only for a key
# not listed here. A key listed here (or in _SPECIAL, _METERS or FIXED_KEYS) is "known"; whether
# its device starts used is decided by used_at_creation.
_NAMES = {
    # Named as the delayed-start unit is ("start_at_name", "ready_at_name"), so a log line names
    # what the card shows.
    START_IN: ("Start at", "Start om"),
    FINISH_IN: ("Ready at", "Klaar om"),
    "BSH.Common.Option.Duration": ("Duration", "Duur"),
    "BSH.Common.Option.EnergyForecast": ("Energy forecast", "Energieprognose"),
    "BSH.Common.Option.WaterForecast": ("Water forecast", "Waterprognose"),
    "BSH.Common.Option.SmartEnergyService.SmartStartEnabled": ("Smart start", "Slimme start"),
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
    "Cooking.Oven.Event.EcolysisRequired": (
        "Ecolysis cleaning needed",
        "Ecolysis-reiniging nodig",
    ),
    "Cooking.Oven.Event.PreheatFinished": ("Preheat finished", "Voorverwarmen klaar"),
    "Cooking.Oven.Status.LastRemoteStartRelease": (
        "Remote start last enabled",
        "Start op afstand laatst vrijgegeven",
    ),
    "Cooking.Oven.Event.RegularPreheatFinished": (
        "Regular preheat finished",
        "Normaal voorverwarmen klaar",
    ),
    # Lifetime totals of the appliance ("All"), not the last run.
    "BSH.Common.Status.Program.All.Energy.Consumed": (
        "Total energy used",
        "Totaal energieverbruik",
    ),
    "BSH.Common.Status.Program.All.Water.Consumed": ("Total water used", "Totaal waterverbruik"),
    "LaundryCare.Washer.Status.Detergent.All.Consumed": ("Total detergent", "Totaal wasmiddel"),
    "LaundryCare.Washer.Status.Softener.All.Consumed": ("Total softener", "Totaal wasverzachter"),
    "Dishcare.Dishwasher.Status.ProgramPhase": ("Program phase", "Programmafase"),
    "Dishcare.Dishwasher.Status.EcoDryActive": ("Eco dry active", "Eco drogen actief"),
    "LaundryCare.Dryer.Status.ConnectedDry.OperationStatus": (
        "Connected Dry status",
        "Connected Dry-status",
    ),
    "LaundryCare.Common.Option.ReferToProgram": ("Program from washer", "Overgenomen wasprogramma"),
    # Option names as the Home Connect app shows them in Dutch; English keeps the brand's names.
    "Dishcare.Dishwasher.Option.BrillianceDry": ("Brilliance Dry", "GlansDroog"),
    "Dishcare.Dishwasher.Option.HygienePlus": ("Hygiene Plus", "HygiënePlus"),
    "Dishcare.Dishwasher.Option.IntensivZone": ("Intensive Zone", "IntensiveZone"),
    "Dishcare.Dishwasher.Option.SilenceOnDemand": ("Silence on demand", "Geluid dempen"),
    "Dishcare.Dishwasher.Option.VarioSpeedPlus": ("VarioSpeed Plus", "VarioSpeedPlus"),
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
    # Programs whose Home Connect name carries a typo ("MIx", "Super Quick 40`").
    "LaundryCare.Dryer.Program.InBasket": ("Wool in drying basket", "Wol in droogkorf"),
    "LaundryCare.Dryer.Program.Mix": ("Mix", "Mix"),
    "LaundryCare.Dryer.Program.Super40": ("Super Quick 40", "Super Quick 40"),
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

# Enum values whose Home Connect name is English in every language. They win over that name.
_VALUE_NAMES = {
    "BSH.Common.EnumType.AmbientLightColor.CustomColor": ("Custom colour", "Eigen kleur"),
}
# Numbered enum values Home Connect usually sends no name for (most ambient light colours), so
# the number is what tells them apart. A name Home Connect does send wins ("cold white").
_NUMBERED_VALUES = {
    "BSH.Common.EnumType.AmbientLightColor.Color": ("Colour {n}", "Kleur {n}"),
}
# Units of measure as shown on a device; the unit sent to Home Connect is never translated.
_UNITS = {"calls": ("calls", "aanroepen")}


def pick(pair: tuple[str, str], lang: str) -> str:
    return pair[1] if lang == "nl" else pair[0]


def word(name: str, lang: str) -> str:
    return pick(_WORDS[name], lang)


def description(name: str, lang: str, control: bool = False, **values: str) -> str:
    """A device description; `control` adds that the device works only while Allow control is on
    (a user decision: said in words, the device type stays the same)."""
    words = {key: pick(pair, lang) for key, pair in _WORDS.items()}
    text = pick(_DESCRIPTIONS[name], lang).format(**words, **values)
    return f"{text} {pick(_DESCRIPTIONS['control_off'], lang)}" if control else text


def note(name: str, lang: str, **values: str) -> str:
    return pick(_NOTES[name], lang).format(**values)


def sign_in_state(state: str) -> str:
    return _SIGN_IN_STATES.get(state, state.lower())


def state_name(state: str, lang: str) -> str:
    return pick(_STATES[state], lang) if state in _STATES else humanize(state)


def humanize(key: str) -> str:
    last = key.rsplit(".", 1)[-1]
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", last).split()
    return " ".join(words).capitalize() if words else key


def special(key: str) -> KeyInfo | None:
    return _SPECIAL.get(key)


def meter(key: str) -> MeterInfo | None:
    return _METERS.get(key)


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


# Named values that are diagnostics or repeat another device: created unused, so they stay off the
# dashboard; a user who wants one adds it.
_DIAGNOSTIC = frozenset(
    {
        "BSH.Common.Status.InteriorIlluminationActive",
        "BSH.Common.Status.BackendConnected",
        "BSH.Common.Status.RemoteControlStartAllowedSince",
        "Cooking.Oven.Status.LastRemoteStartRelease",
        "Dishcare.Dishwasher.Status.EcoDryActive",
        "BSH.Common.Option.EstimatedTotalProgramTime",
        "BSH.Common.Option.EnergyForecast",
        "BSH.Common.Option.WaterForecast",
        "BSH.Common.Option.SmartEnergyService.SmartStartEnabled",
        "Dishcare.Dishwasher.Option.SilenceOnDemand",
    }
)
# Messages nobody notifies on: feedback prompts (".Request") and the dryer's cool-down.
_QUIET_EVENTS = frozenset({"LaundryCare.Dryer.Event.CoolDownPhaseRunning"})


def used_at_creation(key: str, main_setting: bool = False) -> bool:
    """Whether a value's device starts used (spec 6.4). dzVents and Lua load only used devices,
    so a device a notification or script hangs on must start used, and one that only clutters
    the dashboard must not. Every message is used, named or not, except prompts nobody acts on;
    a setting only when it is the appliance's main control (`main_setting`); anything else when
    it is named and not a diagnostic. Domoticz applies Used at creation only, so a user's own
    choice is kept."""
    if ".Event." in key:
        return not (key.endswith(".Request") or key in _QUIET_EVENTS)
    if ".Setting." in key:
        return main_setting
    return known(key) and key not in _DIAGNOSTIC


def known(key: str) -> bool:
    return (
        key in FIXED_KEYS
        or key in _SPECIAL
        or key in _METERS
        or key in _NAMES
        or is_favourite(key)
        or is_compartment_door(key)
    )


def catalog_name(key: str, lang: str) -> str | None:
    """The catalog's own name for `key`, or None when the catalog does not name it."""
    info = _SPECIAL.get(key)
    if info:
        return pick(info.names, lang)
    return pick(_NAMES[key], lang) if key in _NAMES else None


def value_name(names: dict[str, str], value: str, lang: str) -> str | None:
    """The name of an enum value: the catalog's own, else Home Connect's (`names`), else a
    numbered fallback; None when nothing names it."""
    if value in _VALUE_NAMES:
        return pick(_VALUE_NAMES[value], lang)
    if names.get(value):
        return names[value]
    match = re.fullmatch(r"(.*?)(\d+)", value)
    if match and match.group(1) in _NUMBERED_VALUES:
        return pick(_NUMBERED_VALUES[match.group(1)], lang).format(n=int(match.group(2)))
    return None


def unit_text(unit: str, lang: str) -> str:
    return pick(_UNITS[unit], lang) if unit in _UNITS else unit


def name_for(app_names: dict[str, str], key: str, lang: str) -> str:
    """The catalog's name, else the one Home Connect reported, else the key humanized."""
    return catalog_name(key, lang) or app_names.get(key) or humanize(key)
