"""Turn raw probe captures into committable fixtures (dev only, never shipped).

Every identifier of the user's appliances is replaced wherever it occurs, in fields, dict keys,
event `uri` values and SSE `id:` lines, because a capture that only scrubbed the haId field leaked
them once already. That covers the whole haId (a Siemens/Bosch haId is BRAND-MODEL-SERIAL, so the
model segment identifies the appliance too), its serial part, the vib, the E-number and the name
the user gave the appliance. Brand and type stay: the replay needs them.

Run: python3 -I tools/capture_fixtures.py SRC DST [FORBIDDEN]
  SRC        a probe capture directory (homeappliances.*.json, one JSON file per appliance,
             optionally stream-*.txt)
  DST        the fixture directory (tests/fixtures)
  FORBIDDEN  where to write the local forbidden-literals list (gitignored, never committed)

The tool refuses to write anything that still contains one of the real identifiers.
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

CHUNK_SIZE_LINE = re.compile(r"^[0-9a-fA-F]{1,8}$")
SSE_FIELDS = ("event", "data", "id", "retry")


def _appliances(listing: dict) -> list[dict]:
    return listing["body"]["data"]["homeappliances"]


def placeholders(listing: dict) -> dict[str, str]:
    """Real identifier to neutral placeholder, longest real value first.

    Longest first, because a vib is part of an oven's haId and of every E-number: replacing the
    vib first would leave the rest of those identifiers behind in a form nothing matches any more.
    """
    mapping: dict[str, str] = {}
    for n, app in enumerate(_appliances(listing), start=1):
        model = f"HCFIXTURE{n:02d}"
        ha_id = app["haId"]
        serial = ha_id.rsplit("-", 1)[-1]
        number = str(n).rjust(len(serial), "0")
        if ha_id.isdigit():
            mapping[ha_id] = number
        else:
            brand = str(app.get("brand", "BRAND")).upper()
            mapping[ha_id] = f"{brand}-{model}-{number}"
            mapping[serial] = number
            for part in ha_id.split("-")[1:-1]:
                mapping.setdefault(part, model)
        enumber = app.get("enumber")
        if isinstance(enumber, str) and enumber:
            mapping[enumber] = f"{model}/00"
            mapping[enumber.split("/", 1)[0]] = model
        vib = app.get("vib")
        if isinstance(vib, str) and vib:
            mapping[vib] = model
    return dict(sorted(mapping.items(), key=lambda item: -len(item[0])))


def forbidden_literals(listing: dict) -> list[str]:
    """Every value that must never appear in a committed fixture byte.

    An appliance name that is just a type or one of the neutral names says nothing about the
    user, and it occurs throughout the fixtures as the type, so it is not listed.
    """
    generic = set(neutral_names(listing).values())
    generic.update(str(app.get("type", "")) for app in _appliances(listing))
    found: set[str] = set()
    for app in _appliances(listing):
        ha_id = app["haId"]
        found.add(ha_id)
        # Model and serial segments; the first segment of a dashed haId is the brand.
        found.update(ha_id.split("-")[1:] if "-" in ha_id else [ha_id])
        for field in ("vib", "enumber"):
            value = app.get(field)
            if isinstance(value, str) and value:
                found.add(value)
        name = app.get("name")
        if isinstance(name, str) and name and name not in generic:
            found.add(name)
        enumber = app.get("enumber")
        if isinstance(enumber, str) and "/" in enumber:
            found.add(enumber.split("/", 1)[0])
    return sorted(found - {""})


def neutral_names(listing: dict) -> dict[str, str]:
    """haId to a neutral appliance name: the type, numbered when a type repeats."""
    apps = _appliances(listing)
    per_type = Counter(str(app.get("type", "Appliance")) for app in apps)
    seen: Counter[str] = Counter()
    names = {}
    for app in apps:
        kind = str(app.get("type", "Appliance"))
        seen[kind] += 1
        names[app["haId"]] = f"{kind} {seen[kind]}" if per_type[kind] > 1 else kind
    return names


def scrub(value, mapping: dict[str, str]):
    if isinstance(value, dict):
        return {scrub(k, mapping): scrub(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, mapping) for v in value]
    if isinstance(value, str):
        for real, fake in mapping.items():
            value = value.replace(real, fake)
        return value
    return value


def rename(entry: dict, names: dict[str, str]) -> dict:
    """Replace a user-set appliance name; it is looked up by the real haId before scrubbing."""
    if "name" in entry and entry.get("haId") in names:
        return {**entry, "name": names[entry["haId"]]}
    return entry


def stream_lines(raw: list[str]) -> list[str]:
    """SSE lines without the HTTP chunk-size lines the raw capture kept."""
    lines = [line for line in raw if not CHUNK_SIZE_LINE.match(line.strip())]
    for line in lines:
        bare = line.strip()
        if bare and not bare.startswith(":") and bare.split(":", 1)[0] not in SSE_FIELDS:
            raise SystemExit("stream capture has a line that is not an SSE field; refusing")
    return lines


def _dump(value) -> str:
    return json.dumps(value, indent=1, ensure_ascii=False) + "\n"


def main(src: str, dst: str, forbidden_path: str | None = None) -> None:
    src_dir, dst_dir = Path(src), Path(dst)
    listing = json.loads(next(src_dir.glob("homeappliances.*.json")).read_text(encoding="utf-8"))
    mapping = placeholders(listing)
    names = neutral_names(listing)
    forbidden = forbidden_literals(listing)
    out: dict[Path, str] = {}

    renamed = {
        **listing,
        "body": {
            **listing["body"],
            "data": {"homeappliances": [rename(a, names) for a in _appliances(listing)]},
        },
    }
    out[dst_dir / "appliances" / "_list.json"] = _dump(scrub(renamed, mapping))
    per_type: Counter[str] = Counter()
    for path in sorted(src_dir.glob("*.json"), key=lambda p: p.name):
        if path.name.startswith("homeappliances"):
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["appliance"] = rename(raw["appliance"], names)
        data = scrub(raw, mapping)
        kind = re.sub(r"[^a-z0-9]", "", str(data["appliance"].get("type", "appliance")).lower())
        per_type[kind] += 1
        out[dst_dir / "appliances" / f"{kind}-{per_type[kind]}.json"] = _dump(data)
    streams = sorted(src_dir.glob("stream-*.txt"))
    if streams:
        lines = stream_lines(json.loads(streams[-1].read_text(encoding="utf-8")))
        out[dst_dir / "stream.json"] = _dump(scrub(lines, mapping))

    # Case-insensitive: a lowercased or uppercased copy of an identifier is still the identifier.
    lowered = [literal.lower() for literal in forbidden]
    leaks = sum(1 for content in out.values() for literal in lowered if literal in content.lower())
    if leaks:
        raise SystemExit(f"{leaks} real identifier occurrences survived scrubbing; nothing written")
    for path, content in out.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if forbidden_path:
        Path(forbidden_path).write_text(_dump(forbidden), encoding="utf-8")
    print(f"wrote fixtures for {len(names)} appliances, {len(forbidden)} forbidden literals")


USAGE = "usage: python3 -I tools/capture_fixtures.py SRC DST [FORBIDDEN]"


def cli(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(USAGE, file=sys.stderr)
        return 2
    main(*argv)
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
