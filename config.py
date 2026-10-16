"""Plugin settings to a typed configuration. Pure: no Domoticz import."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

RECOMMENDED_VERSION = (2026, 4)

_ACCEPT_LANGUAGE = {"en": "en-GB", "nl": "nl-NL"}
_DEBUG_LEVELS = {"0": 0, "1": 1, "2": 2}
# Catches pasted quotes and stray spaces only. The ID is not checked against the Home Connect
# developer portal, so a well-formed but wrong ID still passes here and fails at sign-in.
_CLIENT_ID_SHAPE = re.compile(r"^[A-Za-z0-9._~-]{8,128}$")
_VERSION = re.compile(r"\s*(\d{4})\.(\d+)")


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str = field(repr=False)
    allow_control: bool
    language: str
    accept_language: str
    sign_out: bool
    debug_level: int
    hardware_id: str
    warnings: tuple[str, ...]


def _flag(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def parse_config(params: Mapping[str, str]) -> Config:
    warnings: list[str] = []

    client_id = str(params.get("Username", "")).strip()
    if client_id and not _CLIENT_ID_SHAPE.match(client_id):
        warnings.append("Client ID contains unexpected characters; check for spaces or quotes")

    choice = str(params.get("DeviceLanguage", "auto")).strip().lower() or "auto"
    if choice == "auto":
        domoticz_language = str(params.get("Language", "en")).lower()
        language = "nl" if domoticz_language.startswith("nl") else "en"
    elif choice in _ACCEPT_LANGUAGE:
        language = choice
    else:
        warnings.append(f"unknown device language {choice!r}; using English")
        language = "en"

    raw_debug = str(params.get("DebugLevel", "0")).strip() or "0"
    debug_level = _DEBUG_LEVELS.get(raw_debug)
    if debug_level is None:
        warnings.append(f"unknown debug level {raw_debug!r}; using 0")
        debug_level = 0

    return Config(
        client_id=client_id,
        client_secret=str(params.get("Password", "")).strip(),
        allow_control=_flag(params.get("AllowControl", "false")),
        language=language,
        accept_language=_ACCEPT_LANGUAGE[language],
        sign_out=_flag(params.get("SignOut", "false")),
        debug_level=debug_level,
        hardware_id=str(params.get("HardwareID", "")),
        warnings=tuple(warnings),
    )


def parse_domoticz_version(text: str) -> tuple[int, int] | None:
    """Return (year, minor) from a Domoticz version string, or None if it has no such prefix.

    Release builds report "2026.4", betas append a build suffix, and dev builds report a
    git describe string; only the leading year and minor are needed for the version check.
    """
    match = _VERSION.match(text or "")
    return (int(match.group(1)), int(match.group(2))) if match else None
