"""Simulator write tool (dev only, never shipped).

Sends the plugin's own write requests to the Home Connect simulator, so their shapes are
checked against a live API without writing to a real appliance (spec 9.4). It talks to
simulator.home-connect.com and nothing else. The requests come from control.py, the module the
plugin sends with, so a shape that passes here is the shape the plugin sends.

The simulator has no device flow (spec 3.6), so it is authorized with the authorization-code
flow, once per token lifetime:

  credentials: HC_SIM_CLIENT_ID, HC_SIM_CLIENT_SECRET, HC_SIM_REDIRECT_URI (and HC_SIM_CODE
  for exchange) are read from the domoticz-test harness .env, or from the file named by HC_SIM_ENV
  python3 -I tools/sim_control.py link
      writes the authorization link to a private file and prints that file's path; open the
      link, sign in with the simulator account, and put the code from the redirect URL into the
      same .env file as HC_SIM_CODE
  python3 -I tools/sim_control.py exchange

Then, for example:

  python3 -I tools/sim_control.py list
  python3 -I tools/sim_control.py show HAID available
  python3 -I tools/sim_control.py select HAID PROGRAM_KEY
  python3 -I tools/sim_control.py option HAID OPTION_KEY 40 [--unit °C] [--running]
  python3 -I tools/sim_control.py setting HAID SETTING_KEY true
  python3 -I tools/sim_control.py start HAID [--at 17:30]
  python3 -I tools/sim_control.py stop HAID
  python3 -I tools/sim_control.py command HAID BSH.Common.Command.PauseProgram

A VALUE is read as JSON when it parses (40, true, "x"), else as text. Tokens are kept in
~/.local/state/home-connect-sim/tokens.json (mode 0600) and never printed.
"""

import argparse
import http.client
import json
import os
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

# -I leaves the script's own folder off sys.path; the plugin modules live one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import control  # noqa: E402
import hc_client  # noqa: E402
import model  # noqa: E402

SIM_HOST = "simulator.home-connect.com"
SCOPE = "IdentifyAppliance Monitor Control Settings"
ACCEPT = "application/vnd.bsh.sdk.v1+json"
DEFAULT_ENV = "~/src/domoticz/domoticz-test/scripts/home-connect/.env"
STATE_DIR = Path.home() / ".local" / "state" / "home-connect-sim"
REFRESH_MARGIN_S = 300.0
_SHOW = {
    "status": ("status",),
    "settings": ("settings",),
    "selected": ("programs", "selected"),
    "active": ("programs", "active"),
    "available": ("programs", "available"),
    "commands": ("commands",),
}


def env_value(name: str) -> str:
    path = Path(os.environ.get("HC_SIM_ENV") or DEFAULT_ENV).expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        sys.exit("the HC_SIM_* credentials file cannot be read")
    for line in lines:
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"{name} is missing from the HC_SIM_ENV file")


def connect() -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(SIM_HOST, timeout=30, context=ssl.create_default_context())


def call(
    method: str,
    path: str,
    *,
    token: str = "",
    body: dict | None = None,
    form: dict | None = None,
) -> tuple[int, object]:
    if not path.startswith(("/api/", "/security/oauth/")):
        raise ValueError("not a Home Connect API path")
    headers = {"Accept": ACCEPT}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Accept"] = "application/json"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = ACCEPT
    conn = connect()
    try:
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        status, raw = response.status, response.read()
    finally:
        conn.close()
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode("utf-8", "replace")[:500]


def tokens_path() -> Path:
    return STATE_DIR / "tokens.json"


def write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # The mode given to os.open applies only to a new file: an existing one is narrowed here,
    # before anything is written to it.
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def save_tokens(answer: dict) -> None:
    lifetime = answer.get("expires_in")
    seconds = float(lifetime) if isinstance(lifetime, int | float) else 86400.0
    tokens = {
        "access_token": answer["access_token"],
        "refresh_token": answer.get("refresh_token", ""),
        "expires_at": time.time() + seconds,
    }
    write_private(tokens_path(), json.dumps(tokens))


def token_grant(form: dict) -> None:
    form = {
        **form,
        "client_id": env_value("HC_SIM_CLIENT_ID"),
        "client_secret": env_value("HC_SIM_CLIENT_SECRET"),
    }
    status, answer = call("POST", "/security/oauth/token", form=form)
    if status != 200 or not isinstance(answer, dict) or "access_token" not in answer:
        # The answer is not printed: a refusal can echo the code it refused.
        sys.exit(f"token request failed: status {status}")
    save_tokens(answer)


def access_token() -> str:
    try:
        tokens = json.loads(tokens_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sys.exit("not authorized yet: run link and exchange first")
    if tokens["expires_at"] - time.time() < REFRESH_MARGIN_S:
        token_grant({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
        tokens = json.loads(tokens_path().read_text(encoding="utf-8"))
    return tokens["access_token"]


def get(*segments: str) -> object:
    status, answer = call("GET", hc_client.api_path(*segments), token=access_token())
    if status != 200:
        sys.exit(f"GET {'/'.join(segments[2:]) or segments[0]} answered {status}: {answer}")
    return answer


def send(request: control.Request) -> int:
    status, answer = call(request.method, request.path, token=access_token(), body=request.body)
    print(f"{request.method} {request.path} {json.dumps(request.body, ensure_ascii=False)}")
    print(f"-> {status}")
    if answer is not None:
        print(json.dumps(answer, indent=1, ensure_ascii=False))
    return 0 if status < 300 else 1


def parse_value(raw: str) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def cmd_link() -> int:
    query = urlencode(
        {
            "client_id": env_value("HC_SIM_CLIENT_ID"),
            "response_type": "code",
            "redirect_uri": env_value("HC_SIM_REDIRECT_URI"),
            "scope": SCOPE,
            "state": "sim-control",
        }
    )
    path = STATE_DIR / "authorize-link.txt"
    # The link carries the client ID, which is never printed.
    write_private(path, f"https://{SIM_HOST}/security/oauth/authorize?{query}\n")
    print(f"authorization link written to {path}")
    return 0


def cmd_exchange() -> int:
    token_grant(
        {
            "grant_type": "authorization_code",
            "code": env_value("HC_SIM_CODE"),
            "redirect_uri": env_value("HC_SIM_REDIRECT_URI"),
        }
    )
    print("authorized")
    return 0


def cmd_list() -> int:
    answer = get("homeappliances")
    data = answer.get("data") if isinstance(answer, dict) else None
    for app in (data or {}).get("homeappliances", []):
        print(f"{app.get('type')}\t{app.get('name')}\t{app.get('haId')}\t{app.get('connected')}")
    return 0


def cmd_show(ha_id: str, what: str) -> int:
    print(json.dumps(get("homeappliances", ha_id, *_SHOW[what]), indent=1, ensure_ascii=False))
    return 0


def cmd_start(ha_id: str, at: str) -> int:
    m = model.Model()
    app = m.appliances.setdefault(ha_id, model.Appliance(ha_id, connected=True))
    m.apply_program(
        ha_id, "selected", get("homeappliances", ha_id, "programs", "selected"), m.tick()
    )
    program = app.selected_program
    if program is None:
        print("no program selected")
        return 1
    detail = get("homeappliances", ha_id, "programs", "available", program)
    m.apply_program_detail(ha_id, program, detail)
    held = None
    if at:
        hours, minutes = (int(part) for part in at.split(":"))
        held = hours * 60 + minutes
    built = control.build_start(app, held, time.time(), "en", "simulator appliance", program)
    if isinstance(built, control.Refusal):
        print(f"refused: {built.reason}")
        return 1
    request, note = built
    if note:
        print(note)
    return send(request)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Home Connect simulator write tool (dev only)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("link")
    sub.add_parser("exchange")
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("ha_id")
    show.add_argument("what", choices=sorted(_SHOW))
    select = sub.add_parser("select")
    select.add_argument("ha_id")
    select.add_argument("program")
    option = sub.add_parser("option")
    option.add_argument("ha_id")
    option.add_argument("key")
    option.add_argument("value")
    option.add_argument("--unit", default="")
    option.add_argument("--running", action="store_true")
    setting = sub.add_parser("setting")
    setting.add_argument("ha_id")
    setting.add_argument("key")
    setting.add_argument("value")
    start = sub.add_parser("start")
    start.add_argument("ha_id")
    start.add_argument("--at", default="")
    stop = sub.add_parser("stop")
    stop.add_argument("ha_id")
    command = sub.add_parser("command")
    command.add_argument("ha_id")
    command.add_argument("key")
    args = parser.parse_args(argv)
    if args.command == "link":
        return cmd_link()
    if args.command == "exchange":
        return cmd_exchange()
    if args.command == "list":
        return cmd_list()
    if args.command == "show":
        return cmd_show(args.ha_id, args.what)
    if args.command == "select":
        return send(control.select_request(args.ha_id, args.program))
    if args.command == "option":
        value = parse_value(args.value)
        return send(
            control.option_request(args.ha_id, args.key, value, args.unit, running=args.running)
        )
    if args.command == "setting":
        return send(control.setting_request(args.ha_id, args.key, parse_value(args.value)))
    if args.command == "start":
        return cmd_start(args.ha_id, args.at)
    if args.command == "stop":
        return send(control.stop_request(args.ha_id))
    return send(control.command_request(args.ha_id, args.key))


if __name__ == "__main__":
    sys.exit(main())
