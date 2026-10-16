"""The fixture scrubber, on a synthetic capture with made-up identifiers."""

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "capture_fixtures", Path(__file__).resolve().parent.parent / "tools" / "capture_fixtures.py"
)
assert _SPEC is not None and _SPEC.loader is not None
capture_fixtures = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(capture_fixtures)

DASHED = "BOSCH-ZZ123AB45-0123456789AB"
NUMERIC = "987654321098765432"
FAKE = [
    DASHED,
    "ZZ123AB45",
    "0123456789AB",
    NUMERIC,
    "QQ98X7/01",
    "QQ98X7",
    "ZZ123AB45/02",
    "My kitchen",
]


def _listing() -> dict:
    return {
        "status": 200,
        "body": {
            "data": {
                "homeappliances": [
                    {
                        "brand": "Bosch",
                        "type": "Oven",
                        "haId": DASHED,
                        "vib": "ZZ123AB45",
                        "enumber": "ZZ123AB45/02",
                        "name": "My kitchen",
                        "connected": True,
                    },
                    {
                        "brand": "Bosch",
                        "type": "Washer",
                        "haId": NUMERIC,
                        "vib": "QQ98X7",
                        "enumber": "QQ98X7/01",
                        "name": "Washer",
                        "connected": True,
                    },
                ]
            }
        },
    }


def _capture(entry: dict, status_body: dict) -> dict:
    ok = {"status": 200, "body": {"data": {}}}
    return {
        "appliance": entry,
        "status": {"status": 200, "body": status_body},
        "settings": ok,
        "programs/available": ok,
        "programs/selected": ok,
        "programs/active": ok,
        "commands": ok,
        "program_details": {},
    }


def _write_source(src: Path, extra_status: str = "") -> None:
    listing = _listing()
    src.mkdir()
    (src / "homeappliances.en-GB.json").write_text(json.dumps(listing))
    oven, washer = listing["body"]["data"]["homeappliances"]
    uri = f"/api/homeappliances/{DASHED}/status/BSH.Common.Status.DoorState"
    status = {"data": {"status": [{"key": "BSH.Common.Status.DoorState", "uri": uri}]}}
    if extra_status:
        status["data"]["note"] = extra_status
    (src / "Oven-ZZ123AB45.en-GB.json").write_text(json.dumps(_capture(oven, status)))
    (src / "Washer-QQ98X7.en-GB.json").write_text(json.dumps(_capture(washer, {"data": {}})))
    stream = [
        "1f\r",
        "event:STATUS",
        'data:{"haId":"' + NUMERIC + '","items":[]}',
        "id:" + NUMERIC,
        "",
        "\r",
    ]
    (src / "stream-1.txt").write_text(json.dumps(stream))


def test_every_identifier_is_replaced_everywhere(tmp_path):
    src, dst, forbidden = tmp_path / "src", tmp_path / "dst", tmp_path / "forbidden.json"
    _write_source(src)
    capture_fixtures.main(str(src), str(dst), str(forbidden))
    written = sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*.json"))
    assert written == [
        "appliances/_list.json",
        "appliances/oven-1.json",
        "appliances/washer-1.json",
        "stream.json",
    ]
    content = "".join(p.read_text(encoding="utf-8") for p in dst.rglob("*.json"))
    for literal in FAKE:
        assert literal not in content
    assert set(FAKE) <= set(json.loads(forbidden.read_text()))
    listing = json.loads((dst / "appliances" / "_list.json").read_text())
    oven, washer = listing["body"]["data"]["homeappliances"]
    assert oven["haId"] == "BOSCH-HCFIXTURE01-000000000001"
    assert (oven["vib"], oven["enumber"], oven["name"]) == ("HCFIXTURE01", "HCFIXTURE01/00", "Oven")
    assert washer["haId"] == "000000000000000002"
    assert (oven["brand"], oven["type"]) == ("Bosch", "Oven")
    status = json.loads((dst / "appliances" / "oven-1.json").read_text())["status"]["body"]
    assert DASHED not in status["data"]["status"][0]["uri"]
    lines = json.loads((dst / "stream.json").read_text())
    assert lines[0] == "event:STATUS"
    assert "id:000000000000000002" in lines


def test_refuses_to_write_when_an_identifier_survives(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    # The user's appliance name inside a free-text field is not a known identifier position.
    _write_source(src, extra_status="Bought for My kitchen")
    with pytest.raises(SystemExit, match="survived"):
        capture_fixtures.main(str(src), str(dst))
    assert not dst.exists()


def test_refuses_a_stream_line_that_is_not_an_sse_field(tmp_path):
    with pytest.raises(SystemExit, match="not an SSE field"):
        capture_fixtures.stream_lines(["event:STATUS", "garbage line"])


def test_survival_check_ignores_case(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write_source(src, extra_status="serial " + "0123456789ab")
    with pytest.raises(SystemExit, match="survived"):
        capture_fixtures.main(str(src), str(dst))
    assert not dst.exists()


def test_cli_prints_usage_on_missing_arguments(capsys):
    assert capture_fixtures.cli(["only-a-source"]) == 2
    assert "usage:" in capsys.readouterr().err
