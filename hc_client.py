"""HTTPS client for the Home Connect API (spec 4.2). Standard library only.

One attempt per call: retries belong to the worker, because every attempt must be booked in the
budget before it is sent (spec 3.4: failures and retries count toward the daily total).
"""

import contextlib
import http.client
import json
import math
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import sse
import text

API_HOST = "api.home-connect.com"
ALLOWED_HOSTS = frozenset({API_HOST})
ACCEPT_JSON = "application/vnd.bsh.sdk.v1+json"
DEVICE_AUTH_PATH = "/security/oauth/device_authorization"
TOKEN_PATH = "/security/oauth/token"
REVOKE_PATH = "/security/oauth/revoke"
EVENTS_PATH = "/api/homeappliances/events"
MAX_BODY = 1 << 20
# Must stay below the plugin's 6 s shutdown deadline: abort() cannot interrupt a connect or TLS
# handshake in progress (conn.sock is still None then), so only this timeout bounds that phase
# (spec 7.9). abort() covers everything after it.
REST_TIMEOUT_S = 5.0
# 65 s: above Home Connect's 55 s keep-alive and below the 70 s stream watchdog. The watchdog
# (reconnect after 70 s without a complete event) lives in StreamWorker; this read timeout is the
# socket-level backstop. It applies only once the response headers have arrived.
STREAM_READ_TIMEOUT_S = 65.0
_PATH_LITERALS = frozenset(
    {
        "homeappliances",
        "status",
        "settings",
        "programs",
        "available",
        "selected",
        "active",
        "options",
        "commands",
        "events",
    }
)


class TransportError(Exception):
    """The request did not produce an HTTP answer. The message is only the exception class name,
    so no URL, header or token can reach a log through it."""


@contextlib.contextmanager
def _transport_errors() -> Iterator[None]:
    try:
        yield
    except (OSError, http.client.HTTPException, ValueError, UnicodeError) as exc:
        # http.client puts the offending header value, a bearer token, in ValueError text and
        # UnicodeEncodeError carries the string; only the class name may leave this module.
        raise TransportError(type(exc).__name__) from None


def _check_header_value(value: str | None) -> None:
    if value is None:
        return
    if not value.isascii() or not all(32 <= ord(c) < 127 for c in value):
        raise TransportError("InvalidHeader")


def retry_after_seconds(headers: dict[str, str]) -> float | None:
    """Retry-After from lower-cased headers. Only the delta-seconds form is understood; an
    HTTP-date, a negative or a non-finite value gives None."""
    raw = headers.get("retry-after")
    try:
        value = float(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and math.isfinite(value) and value >= 0 else None


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    duration_ms: int

    def retry_after(self) -> float | None:
        return retry_after_seconds(self.headers)


def api_path(*segments: str) -> str:
    for segment in segments:
        if segment in _PATH_LITERALS or text.valid_ha_id(segment) or text.valid_key(segment):
            continue
        raise ValueError("invalid path segment")
    return "/api/" + "/".join(quote(s, safe="") for s in segments)


def _default_connect(host: str, timeout: float) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(host, timeout=timeout, context=ssl.create_default_context())


def _shutdown(sock) -> None:
    if sock is None:
        return
    # shutdown wakes a thread blocked in recv on this socket; close() from another thread
    # does not (spec 7.9).
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


class Stream:
    """An open event stream. Lines are read from the HTTPResponse, which undoes the chunked
    transfer framing; reading the underlying file object would hand chunk sizes to the parser."""

    # The socket is captured when the request is sent: HTTPConnection may drop its own `sock`
    # reference while the response is still being read, and abort() must still reach it.
    def __init__(self, conn, response, sock, headers: dict[str, str] | None = None):
        self._conn = conn
        self._response = response
        self._sock = sock
        self.status: int = response.status
        self.headers: dict[str, str] = headers or {}

    def retry_after(self) -> float | None:
        return retry_after_seconds(self.headers)

    def readline(self, limit: int = sse.MAX_LINE + 1) -> bytes:
        """Read one line, at most `limit` bytes, so the parser's line cap fires before an
        oversized line is buffered whole."""
        with _transport_errors():
            return self._response.readline(limit)

    def abort(self) -> None:
        _shutdown(self._sock)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._conn.close()


class HCClient:
    def __init__(
        self,
        host: str = API_HOST,
        *,
        connect: Callable[[str, float], Any] | None = None,
        timeout: float = REST_TIMEOUT_S,
    ):
        if host not in ALLOWED_HOSTS:
            raise ValueError("host not allowed")
        self._host = host
        self._connect = connect or _default_connect
        self._timeout = timeout
        self._lock = threading.Lock()
        self._active: Any = None
        self._aborted = False

    def _register(self, conn) -> None:
        """Make a connected conn reachable by abort(), or refuse it if abort() already ran.

        Checked under the same lock abort() takes, so either abort() sees this conn (its socket
        exists, connect has returned) or the flag is visible here. That closes the window in
        which a stop landing during connect would otherwise go unnoticed.
        """
        with self._lock:
            if self._aborted:
                raise TransportError("Aborted")
            self._active = conn

    def _unregister(self) -> None:
        with self._lock:
            self._active = None

    def _headers(self, token, accept, accept_language) -> dict[str, str]:
        _check_header_value(token)
        _check_header_value(accept_language)
        headers = {"Accept": accept, "User-Agent": "domoticz-home-connect"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if accept_language:
            headers["Accept-Language"] = accept_language
        return headers

    def request(
        self, method, path, *, token=None, json_body=None, form=None, accept_language=None
    ) -> Response:
        accept = "application/json" if path.startswith("/security/") else ACCEPT_JSON
        headers = self._headers(token, accept, accept_language)
        body = None
        if form is not None:
            body = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = ACCEPT_JSON
        started = time.monotonic()
        conn = self._connect(self._host, self._timeout)
        try:
            with _transport_errors():
                conn.connect()
                self._register(conn)
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read(MAX_BODY + 1)
                if len(data) > MAX_BODY:
                    raise TransportError("ResponseTooLarge")
                response_headers = {k.lower(): v for k, v in resp.getheaders()}
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return Response(resp.status, response_headers, data, elapsed_ms)
        finally:
            self._unregister()
            with contextlib.suppress(OSError):
                conn.close()

    def open_stream(self, token: str, accept_language: str | None = None) -> Stream:
        headers = self._headers(token, "text/event-stream", accept_language)
        # Connect, TLS handshake and the wait for the response headers get the short timeout;
        # only reading the open stream waits out its keep-alive interval.
        conn = self._connect(self._host, REST_TIMEOUT_S)
        try:
            with _transport_errors():
                conn.connect()
                self._register(conn)
                conn.request("GET", EVENTS_PATH, headers=headers)
                sock = conn.sock
                response = conn.getresponse()
                response_headers = {k.lower(): v for k, v in response.getheaders()}
                with self._lock:
                    # From here only Stream.abort() reaches this socket.
                    self._active = None
                    if self._aborted:
                        raise TransportError("Aborted")
                if sock is not None:
                    sock.settimeout(STREAM_READ_TIMEOUT_S)
                return Stream(conn, response, sock, response_headers)
        except TransportError:
            with contextlib.suppress(OSError):
                conn.close()
            raise
        finally:
            self._unregister()

    def abort(self) -> None:
        """Interrupt the call in progress and refuse every later one. Permanent: a client is
        aborted only when its worker stops, and a new worker gets a new client. A connect already
        in progress runs to completion or REST_TIMEOUT_S, then the call raises
        TransportError("Aborted") before anything is sent. An open stream is aborted with
        Stream.abort()."""
        with self._lock:
            self._aborted = True
            conn = self._active
        if conn is not None:
            _shutdown(getattr(conn, "sock", None))
