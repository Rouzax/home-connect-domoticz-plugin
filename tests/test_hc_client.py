import http.client
import http.server
import socket
import sys
import threading
import time

import pytest

import hc_client


class FakeResponse:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self._body = body
        self._headers = headers or {}

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def getheaders(self):
        return list(self._headers.items())


class FakeConn:
    def __init__(self, response=None, exc=None):
        self.response = response or FakeResponse()
        self.exc = exc
        self.sent = None
        self.sock = None
        self.closed = False
        self.connects = 0

    def connect(self):
        self.connects += 1

    def request(self, method, path, body=None, headers=None):
        if self.exc:
            raise self.exc
        self.sent = (method, path, body, headers)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def client_with(conn):
    return hc_client.HCClient(connect=lambda host, timeout: conn)


def test_host_allowlist():
    with pytest.raises(ValueError):
        hc_client.HCClient("evil.example")


def test_api_path_quotes_and_validates():
    assert (
        hc_client.api_path("homeappliances", "SIEMENS-X-1", "status")
        == "/api/homeappliances/SIEMENS-X-1/status"
    )
    assert (
        hc_client.api_path("homeappliances", "H1", "programs", "available", "A.B.C")
        == "/api/homeappliances/H1/programs/available/A.B.C"
    )
    for bad in ["../x", "a/b", "a?b", "", "..", "."]:
        with pytest.raises(ValueError):
            hc_client.api_path("homeappliances", bad, "status")


def test_get_sends_bearer_and_language_and_returns_body():
    conn = FakeConn(FakeResponse(200, b'{"data": 1}', {"Retry-After": "7"}))
    resp = client_with(conn).request(
        "GET", "/api/homeappliances", token="TOK", accept_language="nl-NL"
    )
    method, path, body, headers = conn.sent
    assert (method, path, body) == ("GET", "/api/homeappliances", None)
    assert headers["Authorization"] == "Bearer TOK"
    assert headers["Accept-Language"] == "nl-NL"
    assert headers["Accept"] == hc_client.ACCEPT_JSON
    assert resp.body == b'{"data": 1}'
    assert resp.retry_after() == 7.0
    assert conn.closed


def test_form_post_for_oauth():
    conn = FakeConn()
    client_with(conn).request(
        "POST", hc_client.TOKEN_PATH, form={"grant_type": "refresh_token", "refresh_token": "R"}
    )
    _, _, body, headers = conn.sent
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert body == b"grant_type=refresh_token&refresh_token=R"


def test_204_empty_body_is_fine():
    resp = client_with(FakeConn(FakeResponse(204, b""))).request(
        "PUT", "/api/x", token="T", json_body={}
    )
    assert (resp.status, resp.body) == (204, b"")


def test_network_errors_become_transport_error_without_details():
    conn = FakeConn(exc=ConnectionRefusedError("secret-host-detail"))
    with pytest.raises(hc_client.TransportError) as info:
        client_with(conn).request("GET", "/api/x", token="TOKEN-VALUE")
    assert str(info.value) == "ConnectionRefusedError"


def test_oversized_body_is_refused():
    conn = FakeConn(FakeResponse(200, b"x" * (hc_client.MAX_BODY + 5)))
    with pytest.raises(hc_client.TransportError):
        client_with(conn).request("GET", "/api/x", token="T")


class _QuietHTTPServer(http.server.HTTPServer):
    # Tests abort client connections on purpose, so these resets are expected, not handler bugs.
    def handle_error(self, request, client_address):
        benign = (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)
        if isinstance(sys.exc_info()[1], benign):
            return
        super().handle_error(request, client_address)


class _ChunkedSSE(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        payload = b'event:KEEP-ALIVE\ndata:\n\nevent:STATUS\ndata:{"items":[]}\nid:H\n\n'
        # Split mid-line across chunks, as the server does in practice.
        for part in (payload[:7], payload[7:30], payload[30:]):
            self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")

    def log_message(self, *args):
        pass


def test_stream_readline_decodes_chunking_over_a_real_socket():
    server = _QuietHTTPServer(("127.0.0.1", 0), _ChunkedSSE)
    threading.Thread(target=server.handle_request, daemon=True).start()
    port = server.server_address[1]
    client = hc_client.HCClient(
        connect=lambda host, timeout: http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    )
    stream = client.open_stream("TOKEN")
    lines = [stream.readline() for _ in range(7)]
    stream.close()
    server.server_close()
    # Exactly the SSE lines: no chunk-size lines such as "7" or "17" in between.
    assert lines == [
        b"event:KEEP-ALIVE\n",
        b"data:\n",
        b"\n",
        b"event:STATUS\n",
        b'data:{"items":[]}\n',
        b"id:H\n",
        b"\n",
    ]


def test_rest_timeout_is_below_shutdown_deadline():
    assert hc_client.REST_TIMEOUT_S < 6.0


def test_stream_readline_honours_limit_and_exposes_status():
    class R:
        status = 200

        def readline(self, n):
            self.n = n
            return b"x" * n

    conn, r = FakeConn(), R()
    stream = hc_client.Stream(conn, r, None)
    assert stream.status == 200
    assert len(stream.readline(10)) == 10 and r.n == 10
    stream.readline()
    assert r.n == hc_client.sse.MAX_LINE + 1
    stream.abort()
    stream.close()
    assert conn.closed


def test_stream_read_error_is_transport_error():
    class R:
        status = 200

        def readline(self, n):
            raise TimeoutError("secret")

    with pytest.raises(hc_client.TransportError) as info:
        hc_client.Stream(FakeConn(), R(), None).readline()
    assert str(info.value) == "TimeoutError"


def test_open_stream_failure_is_transport_error_and_closes():
    conn = FakeConn(exc=ConnectionResetError("x"))
    with pytest.raises(hc_client.TransportError):
        client_with(conn).open_stream("T")
    assert conn.closed


@pytest.mark.parametrize("bad", ["abc\r\nX: y", "tok€en", "tok\x00en", "tok\x7fen"])
def test_bad_header_values_never_leak_the_token(bad):
    for call in (
        lambda c: c.request("GET", "/api/x", token=bad),
        lambda c: c.open_stream(bad),
        lambda c: c.request("GET", "/api/x", token="T", accept_language=bad),
    ):
        with pytest.raises(hc_client.TransportError) as info:
            call(client_with(FakeConn()))
        for shown in (str(info.value), repr(info.value)):
            assert bad not in shown and "tok" not in shown.lower().replace("transporterror", "")
        assert str(info.value) == "InvalidHeader"


def test_value_and_unicode_errors_from_the_connection_lose_their_text():
    for exc in (
        ValueError("Invalid header value b'Bearer SECRET'"),
        UnicodeEncodeError("latin-1", "SECRET", 0, 1, "x"),
    ):
        with pytest.raises(hc_client.TransportError) as info:
            client_with(FakeConn(exc=exc)).request("GET", "/api/x", token="T")
        assert "SECRET" not in str(info.value) + repr(info.value)
        with pytest.raises(hc_client.TransportError) as info:
            client_with(FakeConn(exc=exc)).open_stream("T")
        assert "SECRET" not in str(info.value) + repr(info.value)


def test_open_stream_connects_short_then_reads_long():
    class Sock:
        timeout = None

        def settimeout(self, t):
            self.timeout = t

    seen = []
    conn = FakeConn()
    conn.sock = Sock()
    conn.response.readline = lambda n: b""
    client = hc_client.HCClient(connect=lambda host, timeout: (seen.append(timeout), conn)[1])
    client.open_stream("T")
    assert seen == [hc_client.REST_TIMEOUT_S]
    assert conn.sock.timeout == hc_client.STREAM_READ_TIMEOUT_S == 65.0


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "-5", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_retry_after_rejects_non_finite_negative_and_dates(raw):
    assert hc_client.Response(429, {"retry-after": raw}, b"", 0).retry_after() is None


def test_abort_is_sticky_and_refuses_later_calls_before_sending():
    conn = FakeConn()
    client = client_with(conn)
    client.abort()
    for call in (
        lambda: client.request("GET", "/api/x", token="T"),
        lambda: client.open_stream("T"),
    ):
        with pytest.raises(hc_client.TransportError) as info:
            call()
        assert str(info.value) == "Aborted"
    assert conn.sent is None and conn.closed


def test_abort_during_connect_stops_the_call_before_it_sends():
    for open_call in (
        lambda c: c.request("GET", "/api/x", token="T"),
        lambda c: c.open_stream("T"),
    ):
        conn = FakeConn()
        client = client_with(conn)
        conn.connect = client.abort  # stop lands while the connect is still in progress
        with pytest.raises(hc_client.TransportError) as info:
            open_call(client)
        assert str(info.value) == "Aborted"
        assert conn.sent is None


def test_open_stream_waits_for_headers_with_the_short_timeout_and_exposes_them():
    class Sock:
        timeout = None

        def settimeout(self, t):
            self.timeout = t

    conn = FakeConn(FakeResponse(429, b"", {"Retry-After": "90"}))
    conn.sock = Sock()
    conn.sock.timeout = hc_client.REST_TIMEOUT_S
    at_headers = []
    original = conn.getresponse

    def getresponse():
        at_headers.append(conn.sock.timeout)
        return original()

    conn.getresponse = getresponse
    stream = client_with(conn).open_stream("T")
    assert at_headers == [hc_client.REST_TIMEOUT_S]
    assert conn.sock.timeout == hc_client.STREAM_READ_TIMEOUT_S
    assert stream.headers == {"retry-after": "90"}
    assert stream.retry_after() == 90.0
    assert hc_client.Stream(conn, FakeResponse(200), None).retry_after() is None


def _silent_server():
    """Accepts one connection, reads the request and never answers."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        conn.recv(4096)
        time.sleep(5)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return server, server.getsockname()[1]


def test_open_stream_gives_up_on_missing_headers_after_the_short_timeout(monkeypatch):
    monkeypatch.setattr(hc_client, "REST_TIMEOUT_S", 0.3)
    server, port = _silent_server()
    client = hc_client.HCClient(
        connect=lambda host, timeout: http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    )
    started = time.monotonic()
    with pytest.raises(hc_client.TransportError):
        client.open_stream("T")
    assert time.monotonic() - started < 2
    server.close()


def test_abort_interrupts_open_stream_waiting_for_headers():
    server, port = _silent_server()
    client = hc_client.HCClient(
        connect=lambda host, timeout: http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    )
    threading.Timer(0.3, client.abort).start()
    started = time.monotonic()
    with pytest.raises(hc_client.TransportError):
        client.open_stream("T")
    assert time.monotonic() - started < 2
    server.close()
