"""Server-sent events parsing for the Home Connect event stream (spec 3.3). Pure.

Lines arrive from HTTPResponse.readline, which has already removed the HTTP chunk framing. The
caller reads with a limit of MAX_LINE + 1 bytes, so a line longer than MAX_LINE is detected here
rather than silently split into two fields.
"""

import json
from dataclasses import dataclass

import errors

MAX_LINE = 65536
MAX_EVENT = 65536


class Overflow(Exception):
    """A line or event exceeded its cap; the stream is dropped and reconnected (spec 7.10)."""


@dataclass(frozen=True)
class Event:
    type: str
    data: str
    id: str


class Parser:
    def __init__(self):
        self._reset()

    def _reset(self):
        self._type = ""
        self._data: list[str] = []
        self._id = ""
        # Joined size of the pending data lines, newlines included, so the cap bounds the
        # string that will be dispatched rather than just the sum of field bodies.
        self._size = 0
        self._seen = False

    def feed(self, line: bytes) -> Event | None:
        if len(line) > MAX_LINE:
            self._reset()
            raise Overflow("line")
        # Errors "replace" keeps a stray byte from raising mid-stream; the decoded text is only
        # ever compared, stored as data, or handed to the JSON decoder.
        decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if decoded == "":
            if not self._seen:
                return None
            event = Event(self._type, "\n".join(self._data), self._id)
            self._reset()
            return event
        if decoded.startswith(":"):
            return None
        name, _, value = decoded.partition(":")
        if value.startswith(" "):
            value = value[1:]
        self._seen = True
        if name == "event":
            self._type = value
        elif name == "data":
            self._size += len(value) + 1
            if self._size > MAX_EVENT:
                self._reset()
                raise Overflow("event")
            self._data.append(value)
        elif name == "id":
            self._id = value
        return None


def items(event: Event) -> list[dict]:
    payload = errors.parse_json(event.data.encode("utf-8"))
    found = payload.get("items") if isinstance(payload, dict) else None
    return [i for i in found if isinstance(i, dict)] if isinstance(found, list) else []


def log_fields(event: Event) -> list[dict[str, object]]:
    """Verbose log fields, one dict per item: the key, the whole value (a structure as JSON) and
    the unit and display value when present. The raw payload is mostly per-item noise (uri,
    timestamp, handling), which pushed the value past the log cap. An event without items gives
    one empty dict, so it still logs once."""
    out: list[dict[str, object]] = []
    for item in items(event):
        value = item.get("value")
        plain = isinstance(value, str | int | float) and not isinstance(value, bool)
        fields: dict[str, object] = {"key": item.get("key", "")}
        fields["value"] = value if plain else json.dumps(value, ensure_ascii=False)
        for name, label in (("unit", "unit"), ("displayvalue", "display")):
            if name in item:
                fields[label] = item[name]
        out.append(fields)
    return out or [{}]
