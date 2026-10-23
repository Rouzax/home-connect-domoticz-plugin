# Contributing

Thanks for helping. This page covers the development setup, the checks every change must pass,
and the rules that keep the plugin safe to run against real appliances.

## Setup

The plugin itself uses only the Python standard library (3.11 or newer). The development tools
are pinned in `requirements-dev.txt`:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pre-commit install
```

## Checks

Every commit must pass all of these; CI runs the same line:

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/pyright && \
  .venv/bin/vulture --min-confidence 80 . .vulture_whitelist.py && .venv/bin/pytest -q
```

- Run one pytest at a time in a checkout: parallel runs share the coverage data file and fail
  with a coverage error that is not a test failure.
- Test coverage has a floor (`fail_under` in `pyproject.toml`). It only moves up: when a change
  raises coverage clearly above the floor, raise the floor in the same change. Never lower it
  to make a change pass.
- A file you change substantially comes up to the current bar: type annotations, tests for
  what you changed, no dead code left behind.

## Layout

- Pure modules (no Domoticz, no I/O of their own): `config`, `model`, `planner`, `catalog`,
  `control`, `coordinator`, `scheduler`, `budget`, `persistence`, `auth`, `errors`, `text`,
  `sse`. Most tests live here.
- Shell: `plugin.py` (Domoticz callbacks and orchestration), `signin.py` (the sign-in flow),
  `workers.py` and `hc_client.py` (network, on worker threads), `domoticz_api.py` (the only
  module besides `plugin.py` that talks to Domoticz).
- Every root-level `*.py` ships to users. Development-only Python belongs in `tests/` or
  `tools/`.

## Rules that protect users

- `domoticz_api.py` never calls `Update` with `TypeName` (it converts a live device in place and
  its history is then read with the wrong meaning) and never deletes a unit (a later device can
  inherit the old one's history). A test guards both.
- A device's type, icon and description are set when it is created and never changed after, so
  a user's own choices survive.
- Selector levels are append-only: scenes and timers store levels by number.
- Test fixtures come from real captures only through `tools/capture_fixtures.py`, which
  replaces every appliance identifier. Never commit a raw capture, an appliance ID, a serial
  number, an email address or a name someone gave their appliance.
- When you develop against a real Home Connect account, use read-only requests. The Home
  Connect simulator (developer portal) is the place to try writes; `tools/sim_control.py`
  exercises the plugin's request shapes against it.

## Testing in Domoticz

A green test suite is not proof that a Domoticz-facing change works. Before a release, run the
plugin on a test Domoticz instance: mount or copy the checkout into its `plugins/homeconnect`
folder, add the hardware with your own Client ID, sign in, and watch the devices and the log
with Debug level set to Basic or Verbose.

## Changes and releases

- Add a line to `CHANGELOG.md` under the unreleased version for anything a user notices.
- The version lives in the `<plugin>` manifest at the top of `plugin.py` and must match the top
  entry of `CHANGELOG.md` (a test checks this).
- Publishing a GitHub release builds the plugin zip and updates the `dist` branch that users
  install from.
