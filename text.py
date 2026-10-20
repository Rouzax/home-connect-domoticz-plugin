"""Making untrusted strings safe for device text, names and logs. Pure.

Appliance and program names come from the Home Connect app, where anyone with access to the
account can type anything. Device sValues are read back by dzVents, the JSON API and themes,
none of which sanitize, so the plugin escapes at the source (spec 7.10).
"""

import html
import re
import unicodedata
from typing import TypeGuard
from urllib.parse import urlsplit

# Format and separator characters are dropped as well as C0/C1 controls: bidi overrides (U+202E)
# can reorder rendered text, and U+0085, U+2028 and U+2029 start a new line in many viewers.
_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
# '|' and ';' separate selector levels and option entries in Domoticz; '<' and '>' are what the
# core's own level editor strips (www/app/DeviceLightEdit.js addLevel).
_LABEL_DROP = re.compile(r"[|;<>]")
_SPACES = re.compile(r"\s+")
# fullmatch, not match: `$` also matches before a trailing newline, which would let "abc\n" pass.
_HA_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
# Must start alphanumeric, so "." and ".." can never pass and become a path segment.
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.]{0,199}")
# Printable ASCII that a Home Connect verification URL can legitimately contain. Backslash, quotes,
# whitespace and angle brackets are excluded: browsers treat '\' as '/', which would let a host
# check on the parsed URL disagree with the host the browser actually connects to.
_AUTH_URL = re.compile(r"https://[A-Za-z0-9\-._~:/?#\[\]@!$()*+,;=%]+")
_AUTH_HOST = re.compile(r"[a-z0-9.-]+")
_AUTH_HOST_SUFFIX = ".home-connect.com"
_AUTH_MAX_LEN = 512


def _strip_controls(text: str) -> str:
    return "".join(
        " " if unicodedata.category(char) in _STRIPPED_CATEGORIES else char for char in text
    )


def escape(value: object) -> str:
    """For element text, which Domoticz renders as HTML. Quotes stay literal because scripts
    read the same sValue; only an attribute value needs them escaped (escape_attribute).
    Expects text already bounded by clean_label; it does no length capping itself."""
    return html.escape(str(value), quote=False)


def escape_attribute(value: object) -> str:
    return html.escape(str(value), quote=True)


def clean_label(value: object, max_len: int = 60) -> str:
    if max_len <= 0:
        return ""
    cleaned = _LABEL_DROP.sub("", _strip_controls(str(value)))
    # Strip again after slicing so a cut at a space does not leave trailing whitespace.
    return _SPACES.sub(" ", cleaned).strip()[:max_len].strip()


def log_value(value: object, max_len: int = 200) -> str:
    cleaned = _strip_controls(str(value))[:max_len].replace("\\", "\\\\").replace('"', '\\"')
    return f'"{cleaned}"'


# TypeGuard lets callers narrow a value read from parsed JSON (typed as object or Any) to str
# after the check, so no cast is needed at the call site.
def valid_ha_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _HA_ID.fullmatch(value) is not None


def valid_key(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _KEY.fullmatch(value) is not None


def auth_link(uri: object) -> str | None:
    """The verification URL Home Connect returned, or None if it is not a plain https link on a
    Home Connect host. It is shown to the user, so a tampered value must never pass through."""
    if not isinstance(uri, str) or len(uri) > _AUTH_MAX_LEN:
        return None
    if _AUTH_URL.fullmatch(uri) is None:
        return None
    try:
        parts = urlsplit(uri)
        host = (parts.hostname or "").lower()
    except ValueError:
        # Malformed bracketed hosts such as "https://[x/" make urlsplit raise.
        return None
    if parts.username or parts.password:
        return None
    if _AUTH_HOST.fullmatch(host) is None:
        return None
    if host != "home-connect.com" and not host.endswith(_AUTH_HOST_SUFFIX):
        return None
    return uri
