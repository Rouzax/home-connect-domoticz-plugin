"""Committed fixtures carry no identifier of the user's appliances (spec 9.2)."""

import json
import re
import subprocess
from pathlib import Path

import pytest

# Failing assertions report only counts and file names: pytest's assertion introspection would
# otherwise print the very identifier the check caught into the test log.
FIXTURES = Path(__file__).parent / "fixtures"
REPO = FIXTURES.parent.parent
LOCAL = Path(__file__).parent / "forbidden_literals.local.json"
FILES = sorted(FIXTURES.rglob("*.json"))
APPLIANCES = sorted(p for p in (FIXTURES / "appliances").glob("*.json") if p.name != "_list.json")
CAPTURE_KEYS = {
    "appliance",
    "status",
    "settings",
    "programs/available",
    "programs/selected",
    "programs/active",
    "commands",
    "program_details",
}


def test_fixtures_are_valid_json():
    assert FILES, "no fixture files found"
    for path in FILES:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if path.name == "stream.json":
            assert isinstance(parsed, list)
            assert all(isinstance(line, str) for line in parsed)
        elif path.parent.name == "appliances" and path.name == "_list.json":
            assert isinstance(parsed, dict)
            assert isinstance(parsed["body"]["data"]["homeappliances"], list)
        elif path.parent.name == "appliances":
            assert isinstance(parsed, dict)
            assert set(parsed) == CAPTURE_KEYS, path.name
            assert isinstance(parsed["program_details"], dict)
        else:
            assert isinstance(parsed, dict | list), path.name


def test_the_five_appliances_are_present():
    assert [p.name for p in APPLIANCES] == [
        "dishwasher-1.json",
        "dryer-1.json",
        "oven-1.json",
        "oven-2.json",
        "washer-1.json",
    ]


def test_every_hex_suffix_is_a_placeholder():
    checked = 0
    for path in FILES:
        checked += 1
        suffixes = re.findall(r"-([0-9A-Fa-f]{12})\b", path.read_text(encoding="utf-8"))
        real = sum(1 for s in suffixes if not (s.isdigit() and int(s) < 1000))
        assert real == 0, f"{path.name}: real-looking haId suffix"
    assert checked, "no fixture files found"


def test_every_long_digit_run_is_a_placeholder():
    # Numeric haIds are 18 digits. Epoch timestamps (10 digits) and the fraction of a float such
    # as 23.999999999999996 are not identifiers.
    assert FILES
    for path in FILES:
        runs = re.findall(r"(?<![\d.])\d{13,}", path.read_text(encoding="utf-8"))
        real = sum(1 for run in runs if int(run) >= 1000)
        assert real == 0, f"{path.name}: real-looking numeric identifier"


def test_appliance_identifiers_are_neutral():
    listing = json.loads((FIXTURES / "appliances" / "_list.json").read_text(encoding="utf-8"))
    appliances = listing["body"]["data"]["homeappliances"]
    assert len(appliances) == 5
    for n, app in enumerate(appliances, start=1):
        neutral = (
            re.fullmatch(r"[A-Z]+-HCFIXTURE\d\d-0+\d{1,3}|0+\d{1,3}", app["haId"]) is not None
            and re.fullmatch(r"HCFIXTURE\d\d", app["vib"]) is not None
            and re.fullmatch(r"HCFIXTURE\d\d/00", app["enumber"]) is not None
            and app["name"].split(" ")[0] == app["type"]
        )
        assert neutral, f"appliance {n} in the listing carries a non-placeholder identifier"


NEEDS_LOCAL = pytest.mark.skipif(
    not LOCAL.exists(), reason="local forbidden literals file missing: this sweep proves nothing"
)


def _forbidden() -> list[str]:
    forbidden = [str(literal).lower() for literal in json.loads(LOCAL.read_text(encoding="utf-8"))]
    assert forbidden, "empty forbidden list proves nothing"
    return forbidden


def _hits(forbidden: list[str], text: str) -> int:
    lowered = text.lower()
    return sum(1 for literal in forbidden if literal in lowered)


@NEEDS_LOCAL
def test_no_real_identifier_in_fixtures():
    forbidden = _forbidden()
    assert FILES
    for path in FILES:
        hits = _hits(forbidden, path.read_text(encoding="utf-8"))
        assert hits == 0, f"{path.name}: {hits} forbidden literal(s)"


@NEEDS_LOCAL
def test_no_real_identifier_in_any_tracked_file():
    # Identifiers leak through hand-written tests and docs too, not only through fixtures.
    forbidden = _forbidden()
    listed = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, cwd=REPO, check=True
    ).stdout.decode("utf-8")
    tracked = [name for name in listed.split("\0") if name]
    assert tracked
    scanned = 0
    for name in tracked:
        path = REPO / name
        name_hits = _hits(forbidden, name)
        assert name_hits == 0, f"a tracked file name carries {name_hits} forbidden literal(s)"
        if path.resolve() == LOCAL.resolve() or not path.is_file():
            continue
        content = path.read_bytes()
        if b"\0" in content:
            continue  # binary
        scanned += 1
        hits = _hits(forbidden, content.decode("utf-8", errors="replace"))
        assert hits == 0, f"{name}: {hits} forbidden literal(s)"
    assert scanned


def test_local_literals_file_is_never_tracked():
    tracked = subprocess.run(
        ["git", "ls-files", str(LOCAL)],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=True,
    )
    assert tracked.stdout.strip() == ""
