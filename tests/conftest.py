import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from tests import domoticz_stub  # noqa: E402

# Installed at import (collection) time so `import DomoticzEx` resolves while pytest imports the
# test modules and the modules they pull in.
domoticz_stub.install()

import plugin  # noqa: E402
from tests import fakes  # noqa: E402

CID = "CLIENTID0123456789"
SECRET = "SECRET-VALUE-123"


@pytest.fixture(autouse=True)
def domoticz():
    """Reset the fake Domoticz before every test."""
    mod = domoticz_stub.install()
    if "plugin" in sys.modules:
        sys.modules["plugin"].Devices = mod.Devices
    return mod


class Clock:
    """Plugin monotonic clock that a test can move forward."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.monotonic() + self.offset


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(plugin, "_mono", c)
    return c


@pytest.fixture
def run(domoticz, monkeypatch):
    made = {"clients": []}

    def client():
        made["clients"].append(fakes.NoClient())
        return made["clients"][-1]

    def req(client_, b, results, generation, lang):
        made["req"] = fakes.FakeRequestWorker(client_, b, results, generation, lang)
        return made["req"]

    def stream(client_, b, results, generation, lang):
        made["stream"] = fakes.FakeStreamWorker(client_, b, results, generation, lang)
        return made["stream"]

    monkeypatch.setattr(plugin, "_new_request_worker", req)
    monkeypatch.setattr(plugin, "_new_stream_worker", stream)
    monkeypatch.setattr(plugin, "_new_client", client)

    class FakeChecker:
        def __init__(self, repo, current, channel, results):
            self.repo, self.current, self.channel, self.results = repo, current, channel, results
            self.started = self.stopped = False

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return False

    def checker(repo, current, channel, results):
        made["checker"] = FakeChecker(repo, current, channel, results)
        return made["checker"]

    monkeypatch.setattr(plugin, "_new_update_checker", checker)

    def start(**params):
        base = {
            "HardwareID": "1",
            "Username": CID,
            "Password": SECRET,
            "Language": "en",
            "DomoticzVersion": "2026.4",
            "DeviceLanguage": "en",
            "DebugLevel": "2",
        }
        base.update(params)
        plugin.Parameters = base
        plugin.Devices = domoticz.Devices
        plugin.onStart()
        return made

    return start
