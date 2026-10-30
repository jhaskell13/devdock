"""Readiness probes: the only part of DevDock that opens a network connection.

A probe answers one question -- is this service accepting work yet -- and
answers it in DevDock's own words. The target comes from a profile that may
have arrived with a cloned repository, so the rules here are deliberately
narrow:

* Only loopback is reachable. The declared host is re-checked, then resolved,
  and every address it resolves to must be loopback before anything connects.
  The connection is then made to that checked address rather than resolving a
  second time, so what was validated is what is contacted.
* Only plain HTTP, with no proxy, no redirect following, no cookies, no
  credentials, and no response body read at all.
* Every attempt is bounded by the declared timeout.
* Results carry a phrase chosen from this module and an HTTP status code.
  Nothing from the endpoint -- no body, no header, no exception text -- is
  repeated, because a probe target can be made to answer with anything.

The policy here is separated from the socket work the same way command policy
is separated from the adapter: ``Network`` performs, ``run_probe`` decides.
Tests supply a fake ``Network`` and never open a socket.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from devdock import __version__
from devdock.config import (
    MAX_PROBE_TIMEOUT_SECONDS,
    HttpReadiness,
    ReadinessProbeBase,
    TcpReadiness,
    is_loopback,
)

ReadinessProbeUnion = TcpReadiness | HttpReadiness

USER_AGENT = f"devdock/{__version__} (readiness probe)"
MIN_HTTP_STATUS = 100
MAX_HTTP_STATUS = 599

# Every phrase a probe result can carry, so the set is reviewable in one place.
ACCEPTED = "accepted a connection"
REFUSED = "refused the connection"
UNREACHABLE = "could not be reached"
UNRESOLVED = "could not be resolved"
NOT_LOOPBACK = "resolved to an address that is not loopback"
MALFORMED = "did not answer a readable HTTP response"
NO_RESPONSE = "accepted the connection, then closed it without answering"


class MalformedResponse(Exception):
    """The endpoint answered, but not with an HTTP response DevDock could read.

    Carries no detail on purpose: the underlying message can quote the bytes
    the endpoint sent.
    """


class ClosedWithoutAnswering(MalformedResponse):
    """The endpoint accepted the connection and hung up before responding.

    Worth separating from every other transport failure because it is what a
    service does for the whole window between binding its port and being able
    to serve -- the exact window a readiness probe exists to observe. Reported
    as a connection problem it would send someone to look at the network.
    """


@dataclass(frozen=True)
class ProbeResult:
    """Whether one probe attempt passed, and a phrase from this module saying why."""

    passed: bool
    detail: str


class Network(Protocol):
    """The socket operations a probe needs, isolated so tests can replace them.

    Implementations raise ``OSError`` and its subclasses for transport
    failures, and ``MalformedResponse`` when an endpoint answered
    unintelligibly. They never raise anything carrying the endpoint's own text.
    """

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        """Return every address ``host`` resolves to, as literal addresses."""
        ...

    def connect(self, address: str, port: int, *, timeout: float) -> None:
        """Open and immediately close a TCP connection, or raise."""
        ...

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
        """Make one HTTP request and return its status code, reading no body."""
        ...


class RealNetwork:
    """The live implementation. Every entry point here is bounded by a timeout."""

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        literal = _as_ip_address(host)
        if literal is not None:
            # No name to look up, which is the default case and keeps the one
            # unbounded call in this module out of the common path.
            return (host,)
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return tuple(dict.fromkeys(str(info[4][0]) for info in infos))

    def connect(self, address: str, port: int, *, timeout: float) -> None:
        with socket.create_connection((address, port), timeout=timeout):
            pass

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
        request = urllib.request.Request(
            f"http://{_url_authority(address, port)}{path}",
            method=method,
            headers={
                # The address is contacted, but the declared host is what the
                # service is told, so name-based routing still works.
                "Host": _url_authority(host, port),
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Connection": "close",
            },
        )
        try:
            response = _opener().open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            # Not following redirects turns every 3xx into this as well, so the
            # status is read here rather than treated as a failure.
            status = error.code
            error.close()
            return _checked_status(status)
        except urllib.error.URLError as error:
            # URLError is itself an OSError, so it must be handled before the
            # general case below.
            reason = error.reason
            if isinstance(reason, BaseException) and isinstance(reason, OSError):
                raise _translated(reason) from None
            raise MalformedResponse from None
        except http.client.RemoteDisconnected:
            # Both an HTTPException and a ConnectionResetError. Named before
            # the HTTPException case so a hang-up after the request was read
            # is described the same way as one before it, which is what the
            # user sees in both halves of the same startup window.
            raise ClosedWithoutAnswering from None
        except http.client.HTTPException:
            raise MalformedResponse from None
        except OSError as error:
            raise _translated(error) from None
        with response:
            # The body is never read. Only the status is wanted, and a body is
            # attacker-chosen content DevDock has no use for.
            return _checked_status(response.status)


def _translated(error: OSError) -> BaseException:
    """Separate a dropped exchange from a host that could not be reached.

    Once a request is in flight the connection was accepted, so a reset or a
    broken pipe means the endpoint hung up rather than that it is unreachable.
    Both shapes occur: a service that closes before reading the request resets
    the write, and one that closes after reading it produces
    ``RemoteDisconnected``, which is a ``ConnectionResetError`` too.
    """

    if isinstance(error, ConnectionResetError | BrokenPipeError):
        return ClosedWithoutAnswering()
    return error


def _checked_status(status: object) -> int:
    if not isinstance(status, int) or not MIN_HTTP_STATUS <= status <= MAX_HTTP_STATUS:
        raise MalformedResponse
    return status


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Report a redirect as its status instead of following it.

    Following one would let the endpoint choose DevDock's next destination,
    which is the whole thing the loopback rule exists to prevent.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _opener() -> urllib.request.OpenerDirector:
    """An opener that can do exactly one thing: a plain HTTP request.

    Built by listing handlers rather than by taking ``build_opener``'s
    defaults, which include file, FTP, data, and HTTPS handlers this has no
    use for. ``ProxyHandler({})`` is not a default: without it, a ``http_proxy``
    variable in the environment would route a loopback probe through a proxy.
    """

    director = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        _NoRedirects(),
        urllib.request.HTTPErrorProcessor(),
    ):
        director.add_handler(handler)
    return director


def _as_ip_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _url_authority(host: str, port: int) -> str:
    """Render ``host:port`` for a URL, bracketing IPv6 addresses."""

    address = _as_ip_address(host)
    if address is not None and address.version == 6:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def describe_target(probe: ReadinessProbeUnion) -> str:
    """A short, safe label for what a probe contacts.

    Every part comes from the profile, which rejected whitespace and control
    characters in all of them, so this is printable as it stands.
    """

    if isinstance(probe, HttpReadiness):
        return f"http {probe.method} {_url_authority(probe.host, probe.port)}{probe.path}"
    return f"tcp {_url_authority(probe.host, probe.port)}"


def _checked_address(probe: ReadinessProbeBase, network: Network) -> tuple[str, str | None]:
    """Resolve the declared host and return one address proven to be loopback.

    The profile already rejected a non-loopback host, and this checks it again
    before connecting: the two checks protect against different things. The
    first stops a profile naming another machine. The second stops a name that
    passes as loopback but resolves elsewhere, which ``localhost`` can do on a
    host whose name resolution says so.
    """

    if not is_loopback(probe.host):
        return "", NOT_LOOPBACK
    try:
        addresses = network.resolve(probe.host, probe.port)
    except OSError:
        return "", UNRESOLVED
    if not addresses:
        return "", UNRESOLVED
    for address in addresses:
        resolved = _as_ip_address(address)
        if resolved is None or not resolved.is_loopback:
            return "", NOT_LOOPBACK
    return addresses[0], None


def run_probe(probe: ReadinessProbeUnion, *, network: Network | None = None) -> ProbeResult:
    """Make one bounded attempt and report it in this module's vocabulary."""

    transport = RealNetwork() if network is None else network
    timeout = min(probe.timeout, MAX_PROBE_TIMEOUT_SECONDS)
    address, refusal = _checked_address(probe, transport)
    if refusal is not None:
        return ProbeResult(False, refusal)

    if isinstance(probe, TcpReadiness):
        try:
            transport.connect(address, probe.port, timeout=timeout)
        except OSError as error:
            return ProbeResult(False, _transport_detail(error, timeout))
        return ProbeResult(True, ACCEPTED)

    try:
        status = transport.fetch(
            address,
            probe.port,
            host=probe.host,
            path=probe.path,
            method=probe.method,
            timeout=timeout,
        )
    except ClosedWithoutAnswering:  # a subclass, so it must be caught first
        return ProbeResult(False, NO_RESPONSE)
    except MalformedResponse:
        return ProbeResult(False, MALFORMED)
    except OSError as error:
        return ProbeResult(False, _transport_detail(error, timeout))
    if not MIN_HTTP_STATUS <= status <= MAX_HTTP_STATUS:
        return ProbeResult(False, MALFORMED)
    if probe.accepts(status):
        return ProbeResult(True, f"answered {status}")
    return ProbeResult(False, f"answered {status}, which is not accepted")


def _transport_detail(error: OSError, timeout: float) -> str:
    """Name the kind of transport failure. The exception's own text is discarded."""

    if isinstance(error, socket.gaierror):
        return UNRESOLVED
    if isinstance(error, ConnectionRefusedError):
        return REFUSED
    if isinstance(error, TimeoutError):
        return f"did not answer within {timeout:g}s"
    return UNREACHABLE
