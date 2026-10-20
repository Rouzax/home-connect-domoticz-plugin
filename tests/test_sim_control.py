"""The simulator tool, against a fake connection. It never talks to anything but the
simulator, and it sends exactly what control.py builds."""

import importlib.util
import json
import stat
from pathlib import Path

import pytest

import catalog
import control

_SPEC = importlib.util.spec_from_file_location(
    "sim_control", Path(__file__).resolve().parent.parent / "tools" / "sim_control.py"
)
assert _SPEC is not None and _SPEC.loader is not None
sim_control = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sim_control)

HA = "SIMULATOR-OVEN-000000000001"
PROGRAM = "Cooking.Oven.Program.HeatingMode.HotAir"
SETPOINT = "Cooking.Oven.Option.SetpointTemperature"


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._raw = b"" if payload is None else json.dumps(payload).encode()

    def read(self):
        return self._raw


class FakeConnection:
    def __init__(self):
        self.answers = []
        self.sent = []

    def request(self, method, path, body=None, headers=None):
        self.sent.append((method, path, body, dict(headers or {})))

    def getresponse(self):
        return FakeResponse(*self.answers.pop(0))

    def close(self):
        pass


@pytest.fixture
def sim(monkeypatch, tmp_path):
    monkeypatch.setattr(sim_control, "STATE_DIR", tmp_path / "state")
    conn = FakeConnection()
    monkeypatch.setattr(sim_control, "connect", lambda: conn)
    sim_control.save_tokens(
        {
            "access_token": "SIM-ACCESS-SECRET",
            "refresh_token": "SIM-REFRESH-SECRET",
            "expires_in": 3600,
        }
    )
    return conn


def test_only_the_simulator_is_ever_contacted():
    source = Path(sim_control.__file__).read_text(encoding="utf-8")
    assert "api.home-connect.com" not in source
    assert sim_control.SIM_HOST == "simulator.home-connect.com"
    with pytest.raises(ValueError):
        sim_control.call("GET", "https://example.org/x")


def test_tokens_are_private_and_never_printed(sim, capsys):
    assert stat.S_IMODE(sim_control.tokens_path().stat().st_mode) == 0o600
    listing = {"data": {"homeappliances": [{"haId": HA, "type": "Oven", "name": "Oven"}]}}
    sim.answers.append((200, listing))
    assert sim_control.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "Oven" in out and "SECRET" not in out
    assert sim.sent[0][3]["Authorization"] == "Bearer SIM-ACCESS-SECRET"


def test_writes_are_the_control_module_requests(sim):
    sim.answers.append((204, None))
    assert sim_control.main(["option", HA, SETPOINT, "200", "--unit", "°C"]) == 0
    method, path, body, headers = sim.sent[-1]
    expected = control.option_request(HA, SETPOINT, 200, "°C")
    assert (method, path, json.loads(body)) == (expected.method, expected.path, expected.body)
    assert headers["Content-Type"] == sim_control.ACCEPT


def test_start_sends_the_plugin_start_request(sim):
    selected = {"data": {"key": PROGRAM, "options": [{"key": SETPOINT, "value": 180}]}}
    start_in = {
        "key": catalog.START_IN,
        "type": "Int",
        "constraints": {"min": 0, "max": 86340, "stepsize": 60},
    }
    detail = {"data": {"key": PROGRAM, "options": [{"key": SETPOINT, "type": "Double"}, start_in]}}
    sim.answers += [(200, selected), (200, detail), (204, None)]
    assert sim_control.main(["start", HA, "--at", "23:30"]) == 0
    method, path, body, _ = sim.sent[-1]
    payload = json.loads(body)
    assert (method, path) == ("PUT", f"/api/homeappliances/{HA}/programs/active")
    assert payload["data"]["key"] == PROGRAM
    assert [o["key"] for o in payload["data"]["options"]] == [SETPOINT, catalog.START_IN]


def test_a_refused_write_exits_non_zero(sim):
    sim.answers.append((409, {"error": {"key": "SDK.Error.WrongOperationState"}}))
    assert sim_control.main(["stop", HA]) == 1


def test_credentials_come_from_the_env_file_and_never_reach_output(monkeypatch, tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("HC_SIM_CLIENT_ID=SIM-CLIENT-ID\nHC_SIM_REDIRECT_URI=http://localhost/cb\n")
    monkeypatch.setenv("HC_SIM_ENV", str(env))
    monkeypatch.setattr(sim_control, "STATE_DIR", tmp_path / "state")
    assert sim_control.main(["link"]) == 0
    assert "SIM-CLIENT-ID" not in capsys.readouterr().out
    link = (tmp_path / "state" / "authorize-link.txt").read_text()
    assert link.startswith("https://simulator.home-connect.com/")
    assert stat.S_IMODE((tmp_path / "state" / "authorize-link.txt").stat().st_mode) == 0o600
    with pytest.raises(SystemExit):
        sim_control.env_value("HC_SIM_CODE")


def test_an_existing_token_file_is_made_private_before_the_tokens_are_written(sim, monkeypatch):
    path = sim_control.tokens_path()
    path.chmod(0o644)
    modes = []
    real_fdopen = sim_control.os.fdopen

    def fdopen(descriptor, *args, **kwargs):
        modes.append(stat.S_IMODE(sim_control.os.fstat(descriptor).st_mode))
        return real_fdopen(descriptor, *args, **kwargs)

    monkeypatch.setattr(sim_control.os, "fdopen", fdopen)
    sim_control.save_tokens({"access_token": "SIM-ACCESS-SECRET", "expires_in": 60})
    assert modes == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
