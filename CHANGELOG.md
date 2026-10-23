# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0-beta.1] - unreleased

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
