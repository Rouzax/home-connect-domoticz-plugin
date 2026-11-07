# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- A Notifications card (Meldingen) per appliance lists the open messages that need you, most
  serious first: Home Connect's level (alert, warning, hint, info), newest first within a level.
  It reads "No notification" when none are open, takes the colour of the most serious message,
  and keeps listing while the appliance is offline. It is created only on an appliance that has
  sent such a message. Messages that only report that something ended (Program finished, Drying
  finished, Preheat finished) do not need you and are not listed.

### Changed

- Fewer cards on the dashboard for a new appliance: State, Notifications and Program, plus a
  washer's meters, plus the control devices when Allow control is on, plus the few values you
  operate or watch on that kind of appliance (an oven's set temperature and duration, a fridge's
  setpoints and doors).
- State carries the whole run in one line: state, program, phase, progress in steps of 10%, and
  the time it will be ready ("about" when the appliance estimates it). When the appliance waits
  for you (Action required), State names the reason: the most serious open message, with the
  number of others, "Action required: drip tray full (+2)". In other states the messages are
  only on Notifications, so the two cards never say the same thing.
- Program shows the program and only the settings that differ from normal (switches that are
  on, choices other than "off", numbers other than 0), and only those of the program shown. It
  no longer shows when the program will be ready; that is now in State. Scripts that read the
  time from the Program text need to change. On State and Program devices that already exist,
  the description in the edit dialog still says Program shows the ready time, because Domoticz
  descriptions are set only when a device is created.
- New devices for scripts start with a "$" in front of their name, which keeps them off the
  dashboard while dzVents and Lua still see them: Door, Progress, Remaining time, the program
  phase and every message device, and Power and Program choice when the appliance is created
  with Allow control off. Turning Allow control on later does not rename Power and Program
  choice; remove the "$" by hand to show them. The oven's water tank and front panel statuses
  now start not used.
- Devices that already exist are never renamed and keep whether they are used. To get the new
  layout on an existing install, rename these with a "$" in front (scripts keep seeing them):
  Door, Progress, Remaining time, the dishwasher's Program phase and the washer's or dryer's
  Process phase, each message device (such as Program finished or Rinse aid nearly empty), and
  Power and Program choice if you keep Allow control off. The oven's water tank and front panel
  status devices can be unticked under Setup > Devices.

### Fixed

- An oven could show "Inactive, Boost" after a microwave program: the oven can drop off Wi-Fi
  while the microwave runs, so the message that the program ended never arrived. An appliance
  that reports itself inactive or ready now shows no running program.
- The dryer's process phase showed the last run's phase after the program ended and when the
  next one started; it now shows "Not running" between programs.
- A dryer's "Rate your last drying cycle" prompt could stay on for a day; it is now cleared when
  the next program starts.
- Dutch names for the coffee machine's messages, the washer's drum cleaning reminder, the
  dryer's refresher water message, the oven's water tank and front panel, and the oven's heating
  levels. Names and values Home Connect itself sends only in English stay English.
- A unit Home Connect reported once (for example grams on a load recommendation) is kept when a
  later report leaves it out, instead of the device showing no unit.

## [0.1.0-beta.7] - 2026-10-27

### Fixed

- An appliance that was still connecting could get the whole account blocked by Home Connect for
  about 10 minutes: the plugin retried requests that timed out at once and kept sending the
  other requests queued for that appliance. A timed-out request now counts as an error for the
  plugin's error brake and is asked again only after 30 seconds, and once Home Connect says an
  appliance is offline, the other requests queued for it are dropped until it reports back.
- A second Home Connect block that started right after the first one ended was not logged.
- The dishwasher's Program phase kept the last phase ("Drying") after the program ended; it now
  shows "Not running".
- Clearer names, in English and Dutch: the dryer's two "Rate your last drying cycle" prompts
  now say which drying target they rate, and the cool-down, Connected Dry version, refresher
  water, Refresh option, cloud connection, program end and oven heating level have their own
  names instead of Home Connect's.

### Changed

- Fewer devices on the dashboard for a new appliance. Program options start hidden, except the
  oven's set temperature, the duration and the laundry process phase. The dryer's Connected Dry
  status and the washer program a dryer takes over start hidden too. Remote control is shown only
  while Allow control is on, like the Actions and delayed start devices. Devices that already
  exist keep their setting; Remote control follows Allow control the next time you switch it.
- The debug log shows an idle appliance's "no program" answer as `outcome="no_program"` instead
  of an error.

## [0.1.0-beta.6] - 2026-10-26

### Fixed

- When Home Connect refuses to start the sign-in because of the application itself, the Home
  Connect device now says what to change instead of "Code expired": "Client ID is not a Device
  Flow app" for an application registered with another OAuth flow (for example one made for
  another Home Connect plugin), and "Client ID or Secret refused" for an unknown Client ID or a
  secret that does not belong to it. The log line `authorization_start_failed` carries a matching
  `hint`.
- The update check is now safe to copy into plugins that run on Python 3.9.

### Documentation

- Settings: with more than one hardware entry, keep the update check on for one of them.

## [0.1.0-beta.5] - 2026-10-24

### Added

- Update check: once a day the plugin asks GitHub whether a newer version exists and announces
  each new one once, in the log and through every Domoticz notification system (also shown on
  screen). A new Update check setting chooses Automatic (betas while you run a beta), Stable
  releases only, Including betas, or Off.

## [0.1.0-beta.4] - 2026-10-24

### Changed

- Which new devices start shown is decided by what they are for, not by whether the plugin has a
  name for them. Every appliance message is shown (also unnamed ones such as a dryer's water tank
  or a washer's drum cleaning reminder), so notifications and scripts can use it; feedback
  prompts and the dryer's cool-down stay hidden. Diagnostics and values that repeat another
  device (interior light, remote start times, estimated program time, the dishwasher's eco dry,
  smart start, silence on demand and energy and water forecasts) are now created hidden. Only
  new devices follow this; devices that already exist keep their setting.

## [0.1.0-beta.3] - 2026-10-23

### Changed

- The Status code device is created as used and named with a leading "$" (for example
  "$Washer Status code"), so dzVents and Lua scripts see it straight away while it stays off the
  dashboard. dzVents and Lua do not reliably see a device that is not used, which is how earlier
  versions created it. On an existing install the device gets the "$" name by itself (unless you
  renamed it), but stays not used: add it under Setup > Devices. If you had added it to the
  dashboard, the "$" now takes it off; rename it without the "$" to bring it back.

## [0.1.0-beta.2] - 2026-10-23

### Changed

- The plugin key and its folder are now `homeconnectex` instead of `homeconnect`, so the plugin
  is not mistaken for the separate "Bosch Home Connect" plugin. An installation of 0.1.0-beta.1
  is not carried over: delete its hardware and its `homeconnect` folder, install into
  `homeconnectex` and sign in again.

## [0.1.0-beta.1] - 2026-10-23

First public beta.

### Added

- Sign-in with your Home Connect account through the OAuth device flow: the plugin shows a link
  and a code on its account device and in the log; Sign out withdraws its access.
- Monitoring of every appliance on the account: power, door, operation state, a stable status
  code for scripts, selected and active program with progress, remaining time and finish time,
  options, settings, status values and event alerts, in English or Dutch.
- Control, off by default (Allow control): choose a program, change options and settings,
  start, stop, pause, resume and acknowledge, and start later at a chosen time that can be
  moved before it begins. Every command is checked before it is sent, and a refusal names the
  fix.
- On-screen messages: a refused or failed command shows a short message at the top of the
  Domoticz screen with the reason and what to do next (Domoticz 2026.1 or newer; configurable,
  including confirmations or off).
- The washing machine's lifetime energy, water, detergent and softener totals as Domoticz
  meters, with use per day, month and year.
- A rolling 24-hour call budget that keeps the plugin within Home Connect's daily limit, with
  the last calls of the day reserved for token refresh and reconnecting the event stream, so a
  busy day of commands cannot sign the plugin out.
