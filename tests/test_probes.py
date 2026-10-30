"""Probe policy against a fake network, and RealNetwork against real local sockets.

The policy tests never open a socket. The handful that exercise ``RealNetwork``
bind a listener on loopback in the test process itself, so they create nothing
outside it and need no daemon.
"""

from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Iterator

import pytest

from devdock.config import HttpReadiness, TcpReadiness
from devdock.probes import (
    ACCEPTED,
    MALFORMED,
    NO_RESPONSE,
    NOT_LOOPBACK,
    REFUSED,
    UNREACHABLE,
    UNRESOLVED,
    ClosedWithoutAnswering,
    MalformedResponse,
    ProbeResult,
    RealNetwork,
    describe_target,
    run_probe,
)


class FakeNetwork:
    """Scripted resolution and transport; records what a probe asked for."""

    def __init__(
        self,
        *,
        addresses: tuple[str, ...] = ("127.0.0.1",),
        resolve_error: BaseException | None = None,
        outcome: BaseException | int | None = None,
    ) -> None:
        self._addresses = addresses
        self._resolve_error = resolve_error
        self._outcome = outcome
        self.connected: list[tuple[str, int, float]] = []
        self.fetched: list[dict[str, object]] = []

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        if self._resolve_error is not None:
            raise self._resolve_error
        return self._addresses

    def connect(self, address: str, port: int, *, timeout: float) -> None:
        self.connected.append((address, port, timeout))
        if isinstance(self._outcome, BaseException):
            raise self._outcome

    def fetch(
        self,
        address: str,
        port: int,
        *,
        host: str,
        path: str,
        method: str,
        timeout: float,
    ) -> int:
        self.fetched.append(
            {
                "address": address,
                "port": port,
                "host": host,
                "path": path,
                "method": method,
                "timeout": timeout,
            }
        )
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        assert isinstance(self._outcome, int)
        return self._outcome


def tcp(**fields: object) -> TcpReadiness:
    return TcpReadiness.model_validate({"type": "tcp", "port": 5432, **fields})


def http_probe(**fields: object) -> HttpReadiness:
    return HttpReadiness.model_validate({"type": "http", "port": 8080, **fields})


# --- TCP ----------------------------------------------------------------


def test_an_accepted_connection_passes() -> None:
    network = FakeNetwork()

    assert run_probe(tcp(), network=network) == ProbeResult(True, ACCEPTED)
    assert network.connected == [("127.0.0.1", 5432, 2.0)]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ConnectionRefusedError(), REFUSED),
        (TimeoutError(), "did not answer within 2s"),
        (socket.gaierror(), UNRESOLVED),
        (OSError(), UNREACHABLE),
        (ConnectionResetError(), UNREACHABLE),
    ],
)
def test_transport_failures_are_named_from_a_fixed_vocabulary(
    error: BaseException, expected: str
) -> None:
    result = run_probe(tcp(), network=FakeNetwork(outcome=error))

    assert result == ProbeResult(False, expected)


def test_the_declared_timeout_reaches_the_connection() -> None:
    network = FakeNetwork()

    run_probe(tcp(timeout=0.5), network=network)

    assert network.connected[0][2] == 0.5


# --- loopback enforcement -----------------------------------------------


def test_a_name_resolving_off_loopback_is_refused_before_connecting() -> None:
    """The profile check passes 'localhost'; resolution is what catches this."""

    network = FakeNetwork(addresses=("10.0.0.5",))

    result = run_probe(tcp(host="localhost"), network=network)

    assert result == ProbeResult(False, NOT_LOOPBACK)
    assert network.connected == []


def test_one_off_loopback_address_among_several_refuses_the_whole_probe() -> None:
    network = FakeNetwork(addresses=("127.0.0.1", "::1", "192.168.1.5"))

    assert run_probe(tcp(host="localhost"), network=network).detail == NOT_LOOPBACK
    assert network.connected == []


def test_the_address_that_was_checked_is_the_one_contacted() -> None:
    """Connecting by name again would discard what resolution just proved."""

    network = FakeNetwork(addresses=("127.0.0.7", "127.0.0.8"))

    run_probe(tcp(host="localhost"), network=network)

    assert network.connected[0][0] == "127.0.0.7"


def test_a_host_that_does_not_resolve_is_reported_as_such() -> None:
    network = FakeNetwork(resolve_error=socket.gaierror())

    assert run_probe(tcp(host="localhost"), network=network).detail == UNRESOLVED


def test_resolution_returning_nothing_is_not_treated_as_success() -> None:
    assert run_probe(tcp(), network=FakeNetwork(addresses=())).detail == UNRESOLVED


def test_an_unparseable_resolution_result_is_refused() -> None:
    network = FakeNetwork(addresses=("not-an-address",))

    assert run_probe(tcp(), network=network).detail == NOT_LOOPBACK


# --- HTTP ---------------------------------------------------------------


@pytest.mark.parametrize("status", [200, 204, 301, 399])
def test_statuses_in_the_default_range_pass(status: int) -> None:
    result = run_probe(http_probe(), network=FakeNetwork(outcome=status))

    assert result == ProbeResult(True, f"answered {status}")


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_statuses_outside_the_default_range_fail_with_the_code(status: int) -> None:
    result = run_probe(http_probe(), network=FakeNetwork(outcome=status))

    assert result == ProbeResult(False, f"answered {status}, which is not accepted")


def test_a_declared_status_list_decides_instead_of_the_default_range() -> None:
    probe = http_probe(expect_status=[503])

    assert run_probe(probe, network=FakeNetwork(outcome=503)).passed
    assert not run_probe(probe, network=FakeNetwork(outcome=200)).passed


def test_the_request_is_sent_to_the_checked_address_with_the_declared_host() -> None:
    network = FakeNetwork(addresses=("127.0.0.3",), outcome=200)

    run_probe(http_probe(host="localhost", path="/healthz"), network=network)

    assert network.fetched == [
        {
            "address": "127.0.0.3",
            "port": 8080,
            "host": "localhost",
            "path": "/healthz",
            "method": "GET",
            "timeout": 2.0,
        }
    ]


def test_a_head_probe_asks_for_head() -> None:
    network = FakeNetwork(outcome=200)

    run_probe(http_probe(method="HEAD"), network=network)

    assert network.fetched[0]["method"] == "HEAD"


@pytest.mark.parametrize("status", [0, 99, 600, 1000])
def test_a_status_outside_the_http_range_is_not_trusted(status: int) -> None:
    assert run_probe(http_probe(), network=FakeNetwork(outcome=status)).detail == MALFORMED


def test_an_unreadable_response_is_reported_without_its_content() -> None:
    result = run_probe(http_probe(), network=FakeNetwork(outcome=MalformedResponse()))

    assert result == ProbeResult(False, MALFORMED)


def test_a_hang_up_is_reported_as_itself_and_not_as_an_unreadable_response() -> None:
    result = run_probe(http_probe(), network=FakeNetwork(outcome=ClosedWithoutAnswering()))

    assert result == ProbeResult(False, NO_RESPONSE)


def test_http_transport_failures_use_the_same_vocabulary() -> None:
    result = run_probe(http_probe(), network=FakeNetwork(outcome=ConnectionRefusedError()))

    assert result == ProbeResult(False, REFUSED)


def test_no_probe_result_repeats_anything_the_endpoint_said() -> None:
    """Every failure phrase is defined in the module, not taken from the error."""

    hostile = "Set-Cookie: session=hunter2; token=sk_live_abcdef"
    results = [
        run_probe(http_probe(), network=FakeNetwork(outcome=OSError(hostile))),
        run_probe(http_probe(), network=FakeNetwork(outcome=MalformedResponse(hostile))),
        run_probe(tcp(), network=FakeNetwork(outcome=ConnectionRefusedError(hostile))),
        run_probe(tcp(), network=FakeNetwork(resolve_error=socket.gaierror(hostile))),
    ]

    for result in results:
        assert "hunter2" not in result.detail
        assert "sk_live" not in result.detail


# --- labels -------------------------------------------------------------


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        (tcp(), "tcp 127.0.0.1:5432"),
        (tcp(host="::1"), "tcp [::1]:5432"),
        (http_probe(path="/healthz"), "http GET 127.0.0.1:8080/healthz"),
        (http_probe(method="HEAD", host="localhost"), "http HEAD localhost:8080/"),
    ],
)
def test_targets_render_readably(probe: object, expected: str) -> None:
    assert describe_target(probe) == expected  # type: ignore[arg-type]


# --- RealNetwork against real local sockets -----------------------------


@pytest.fixture(scope="module")
def listening_port() -> Iterator[int]:
    """A real TCP listener in this process that accepts and closes.

    Module-scoped: it holds no state between tests, and tearing a server down
    costs half a second that the default suite should not pay once per test.
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        port = server.getsockname()[1]
        stop = threading.Event()

        def serve() -> None:
            server.settimeout(0.2)
            while not stop.is_set():
                try:
                    connection, _ = server.accept()
                except (TimeoutError, OSError):
                    continue
                connection.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield port
        finally:
            stop.set()
            thread.join(timeout=5)


@pytest.fixture(scope="module")
def http_port() -> Iterator[int]:
    """A real HTTP server on loopback that answers a few fixed routes."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - name fixed by the base class
            if self.path == "/moved":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/secret-token-9fd2")
                self.end_headers()
                return
            if self.path == "/teapot":
                self.send_response(418)
                self.end_headers()
                self.wfile.write(b"secret-body-4a1c")
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"secret-body-4a1c")

        def do_HEAD(self) -> None:  # noqa: N802 - name fixed by the base class
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_real_listener_is_reported_ready(listening_port: int) -> None:
    assert run_probe(tcp(port=listening_port)) == ProbeResult(True, ACCEPTED)


def test_a_real_closed_port_is_reported_refused() -> None:
    result = run_probe(tcp(port=_free_port(), timeout=2))

    assert result.detail in {REFUSED, UNREACHABLE}
    assert not result.passed


def test_localhost_really_resolves_to_loopback_on_this_machine() -> None:
    assert RealNetwork().resolve("localhost", 80), "localhost did not resolve"
    assert all(
        __import__("ipaddress").ip_address(address).is_loopback
        for address in RealNetwork().resolve("localhost", 80)
    )


def test_an_address_literal_is_not_sent_through_name_resolution() -> None:
    assert RealNetwork().resolve("127.0.0.5", 80) == ("127.0.0.5",)


def test_a_real_http_server_answering_200_passes(http_port: int) -> None:
    assert run_probe(http_probe(port=http_port)) == ProbeResult(True, "answered 200")


def test_a_real_head_request_passes(http_port: int) -> None:
    assert run_probe(http_probe(port=http_port, method="HEAD")).passed


def test_a_real_redirect_is_reported_not_followed(http_port: int) -> None:
    """Following it would let the endpoint choose where DevDock connects next."""

    result = run_probe(http_probe(port=http_port, path="/moved"))

    assert result == ProbeResult(True, "answered 302")
    assert "secret-token-9fd2" not in result.detail


def test_a_real_error_status_carries_the_code_and_not_the_body(http_port: int) -> None:
    result = run_probe(http_probe(port=http_port, path="/teapot"))

    assert result == ProbeResult(False, "answered 418, which is not accepted")
    assert "secret-body-4a1c" not in result.detail


def test_a_real_error_status_can_be_the_expected_one(http_port: int) -> None:
    probe = http_probe(port=http_port, path="/teapot", expect_status=[418])

    assert run_probe(probe) == ProbeResult(True, "answered 418")


def test_a_real_http_probe_against_a_closed_port_fails_without_raising() -> None:
    result = run_probe(http_probe(port=_free_port(), timeout=2))

    assert not result.passed
    assert result.detail in {REFUSED, UNREACHABLE}


def test_a_listener_that_hangs_up_is_distinguished_from_an_unreachable_one(
    listening_port: int,
) -> None:
    """This is what a service does between binding its port and serving.

    Reported as a transport failure it would read 'could not be reached',
    which sends someone to look at the network instead of at the service.
    """

    result = run_probe(http_probe(port=listening_port, timeout=5))

    assert result == ProbeResult(False, NO_RESPONSE)


def test_a_proxy_variable_in_the_environment_cannot_redirect_a_probe(
    http_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ProxyHandler({}), urllib would honour this and leave the machine."""

    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")

    assert run_probe(http_probe(port=http_port)) == ProbeResult(True, "answered 200")


@pytest.mark.parametrize("behaviour", ["close_immediately", "read_then_close"])
def test_both_halves_of_the_startup_window_are_described_the_same_way(
    behaviour: str,
) -> None:
    """A service hangs up before reading the request, and later after reading it.

    They surface as different exceptions -- a reset on the write, and
    RemoteDisconnected on the read -- but to someone watching a service start
    they are one situation and must read as one.
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        port = int(server.getsockname()[1])

        def serve() -> None:
            connection, _ = server.accept()
            if behaviour == "read_then_close":
                connection.recv(4096)
            connection.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            result = run_probe(http_probe(port=port, timeout=5))
        finally:
            thread.join(timeout=5)

    assert result == ProbeResult(False, NO_RESPONSE)


def test_a_tcp_probe_passes_where_an_http_probe_cannot(listening_port: int) -> None:
    """The reason both probe types exist: an open port is not a served request."""

    assert run_probe(tcp(port=listening_port)).passed
    assert not run_probe(http_probe(port=listening_port, timeout=5)).passed


def test_a_request_the_http_client_rejects_is_a_result_not_a_traceback() -> None:
    """ValueError is neither a transport error nor one of this module's own.

    The profile validator rejects the paths that cause it, but a probe that
    raised would replace a read-only report with a traceback, so the escape
    hatch is closed here too.
    """

    class Raiser(FakeNetwork):
        def fetch(self, *args: object, **kwargs: object) -> int:
            raise ValueError("'ascii' codec can't encode character")

    assert run_probe(http_probe(), network=Raiser()) == ProbeResult(False, MALFORMED)
