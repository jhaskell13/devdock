"""Waiting for a project to become ready, and explaining it when it does not.

The engine polls a snapshot of every service, decides what each one is waiting
for, and stops as soon as the answer is settled. It owns four things no other
module does: when a service's own clock starts, what a dependency condition
means at runtime, which changes are worth failing fast on, and what to say
when a wait runs out.

Everything here is pure with respect to the outside world. Time comes from an
injected ``Clock``, observations from an injected ``Observer``, and
cancellation from a token someone else sets. Nothing in this module runs a
command, opens a socket, or sleeps on a real clock, which is what lets slow
starts, flapping, and deadlines be tested in milliseconds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

from devdock.config import DependencyCondition
from devdock.graph import Dependency, DependencyGraph
from devdock.status import ReadinessSource, ServiceStatus, SourceResult

WaitStatus = Literal[
    "ready",  # every service settled, and each was verified by a source
    "unverified",  # every service settled, but some had nothing to verify them
    "timed_out",  # a service ran out of its own budget
    "deadline_exceeded",  # the whole wait ran out of time
    "failed",  # a service entered a state it cannot come back from
    "regressed",  # a service that had succeeded left that state
    "unsatisfiable",  # a declared condition can never be met
    "cancelled",  # the caller asked to stop
    "unobservable",  # the runtime stopped answering
]

# How many consecutive passing samples a source needs before it is believed.
# A Compose health check has already applied its own retries, so one report is
# enough. A probe has none: an accept during a crash loop can succeed once, so
# a second sample is required to call it stable.
REQUIRED_SUCCESSES: dict[ReadinessSource, int] = {"compose_health": 1, "tcp": 2, "http": 2}

DEFAULT_READY_TIMEOUT = 60.0
DEFAULT_DEADLINE = 300.0
INITIAL_POLL_INTERVAL = 0.25
MAX_POLL_INTERVAL = 2.0

# A container with a health check reports "starting" as soon as it runs, so a
# running container reporting no health at all almost certainly has none. Only
# almost: the first report can lag creation. A service is given this long to
# produce one before a service_healthy wait is called unsatisfiable.
HEALTH_REPORT_GRACE = 5.0

# Consecutive failed observations before the wait gives up. A single failure
# is worth retrying; a daemon that has stopped answering is not worth waiting
# out the global deadline for.
MAX_CONSECUTIVE_ERRORS = 3

# Ready/not-ready changes before a service is called unstable. Reported, not
# disqualifying: the consecutive-success requirement is what decides whether
# it has settled, and a service that flapped early and then steadied is ready.
FLAP_THRESHOLD = 3

_RUNNING = frozenset({"running", "starting", "ready", "unhealthy"})
_SUCCESS_WITHOUT_SOURCES = "running"


class Clock(Protocol):
    """Monotonic time and sleeping, injected so a wait can be tested instantly."""

    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class RealClock:
    """Monotonic rather than wall clock: a wait must not be affected by a clock change."""

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class CancelToken:
    """A flag someone else sets to stop the wait at the next safe point."""

    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


@dataclass(frozen=True)
class Snapshot:
    """One observation of every service, or why it could not be taken."""

    services: tuple[ServiceStatus, ...] = ()
    error: str | None = None


class Observer(Protocol):
    """Produce a current snapshot. Implementations query the runtime and probe."""

    def __call__(self) -> Snapshot: ...


@dataclass
class ServiceProgress:
    """What the wait has learned about one service so far."""

    service: str
    readiness: str = "missing"
    exit_code: int | None = None
    sources: tuple[SourceResult, ...] = ()
    successes: int = 0
    ever_running: bool = False
    running_since: float | None = None
    unblocked_at: float | None = None
    reached_success: bool = False
    succeeded_at: float | None = None
    flaps: int = 0
    blocked_by: tuple[Dependency, ...] = ()

    @property
    def running(self) -> bool:
        return self.readiness in _RUNNING

    @property
    def required_successes(self) -> int:
        return max((REQUIRED_SUCCESSES[item.source] for item in self.sources), default=1)

    @property
    def verified(self) -> bool:
        return bool(self.sources)

    def waited(self, now: float) -> float:
        """Seconds counted against this service's own budget."""

        return 0.0 if self.unblocked_at is None else max(0.0, now - self.unblocked_at)


@dataclass(frozen=True)
class WaitOutcome:
    """The result of one wait, with enough detail to say what happened and why."""

    status: WaitStatus
    elapsed: float
    progress: tuple[ServiceProgress, ...]
    summary: str
    detail: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in {"ready", "unverified"}

    @property
    def unverified_services(self) -> tuple[str, ...]:
        return tuple(
            item.service
            for item in self.progress
            if not item.verified and item.readiness == _SUCCESS_WITHOUT_SOURCES
        )

    def explain(self) -> tuple[str, ...]:
        lines = [f"Waited {self.elapsed:.1f}s."]
        for item in sorted(self.progress, key=lambda value: value.service):
            lines.append(f"  {item.service}: {_progress_line(item, self.elapsed)}")
            for source in item.sources:
                mark = "PASS" if source.passed else "FAIL"
                lines.append(f"    [{mark}] {source.describe()}")
        lines.extend(self.detail)
        return tuple(lines)


def _progress_line(item: ServiceProgress, elapsed: float) -> str:
    if item.blocked_by:
        waiting = ", ".join(f"{edge.depends_on} ({edge.condition})" for edge in item.blocked_by)
        return f"blocked, waiting on {waiting}"
    if item.succeeded_at is not None:
        timing = f" after {item.succeeded_at:.1f}s"
    elif item.unblocked_at is not None:
        timing = f", waiting {elapsed - item.unblocked_at:.1f}s"
    else:
        timing = ""
    flapped = f"; became ready and lost it {item.flaps} time(s)" if item.flaps else ""
    return f"{item.readiness}{timing}{flapped}"


@dataclass
class _Tracker:
    """Mutable per-service state, kept out of the public dataclasses."""

    progress: dict[str, ServiceProgress] = field(default_factory=dict)

    def for_service(self, service: str) -> ServiceProgress:
        if service not in self.progress:
            self.progress[service] = ServiceProgress(service)
        return self.progress[service]


def condition_met(condition: DependencyCondition, item: ServiceProgress) -> bool:
    """Whether a dependency currently satisfies one condition."""

    if condition == "service_started":
        return item.ever_running or item.readiness == "exited_ok"
    if condition == "service_healthy":
        return any(source.source == "compose_health" and source.passed for source in item.sources)
    if condition == "service_completed_successfully":
        return item.readiness == "exited_ok"
    return item.readiness == "ready" or (
        item.readiness == _SUCCESS_WITHOUT_SOURCES and not item.verified
    )


def condition_unsatisfiable(
    condition: DependencyCondition, item: ServiceProgress, now: float
) -> str | None:
    """Why a condition can never be met, or ``None`` while it still might be.

    Reported rather than waited out: a condition that cannot be met would
    otherwise consume the dependent service's whole budget and then blame the
    timeout instead of the cause.
    """

    if condition == "service_healthy":
        reports_health = any(source.source == "compose_health" for source in item.sources)
        running_long_enough = (
            item.running_since is not None and now - item.running_since >= HEALTH_REPORT_GRACE
        )
        if item.running and not reports_health and running_long_enough:
            return (
                f"{item.service} is running but reports no health status, so it has no "
                "health check. Add a healthcheck to it in the Compose file, or depend on "
                "service_started, or give it a readiness probe and depend on service_ready."
            )
    if condition == "service_completed_successfully" and item.readiness == "stopped":
        return (
            f"{item.service} exited with code {item.exit_code}, so it did not complete "
            "successfully. Correct the service, then retry."
        )
    if condition in {"service_healthy", "service_ready"} and item.readiness == "stopped":
        return (
            f"{item.service} exited with code {item.exit_code} and cannot become "
            f"{'healthy' if condition == 'service_healthy' else 'ready'}."
        )
    return None


def _apply(item: ServiceProgress, status: ServiceStatus, now: float, require_ready: bool) -> None:
    """Fold one observation into a service's running history."""

    was_success = _is_success(item, require_ready) if item.readiness != "missing" else False
    item.readiness = status.readiness
    item.exit_code = status.exit_code
    item.sources = status.sources
    if item.running:
        item.ever_running = True
        if item.running_since is None:
            item.running_since = now
    else:
        item.running_since = None

    now_success = _is_success(item, require_ready)
    item.successes = item.successes + 1 if now_success else 0
    if was_success and not now_success and item.reached_success:
        item.flaps += 1


def _is_success(item: ServiceProgress, require_ready: bool) -> bool:
    """Whether this sample is an acceptable resting state for the service."""

    if item.readiness in {"ready", "exited_ok"}:
        return True
    if item.readiness == _SUCCESS_WITHOUT_SOURCES:
        # Running with nothing to check. Accepted by default because there is
        # no further evidence to wait for, and refused under --require-ready
        # because the caller asked to be shown rather than told.
        return not require_ready
    return False


def _failure(item: ServiceProgress) -> str | None:
    """Why a service cannot be waited for any longer, or ``None``."""

    if item.readiness == "stopped":
        return f"{item.service} exited with code {item.exit_code}."
    if item.readiness == "unknown":
        return f"{item.service} is in a state DevDock does not model."
    return None


def _settled(item: ServiceProgress, require_ready: bool) -> bool:
    """Whether a service needs nothing further from the wait.

    A blocked service is never settled, however healthy it looks. Its
    dependency condition was declared for a reason, and a wait that reported
    success while one went unmet would hide exactly what it was asked to
    watch -- a service can be running happily against a database that never
    came up.
    """

    return (
        not item.blocked_by
        and _is_success(item, require_ready)
        and item.successes >= item.required_successes
    )


def _regression(item: ServiceProgress) -> str | None:
    """Why a service that had already succeeded can no longer be trusted.

    Losing readiness is not enough. A service that drops back to ``starting``
    is flapping, which the wait counts and keeps waiting through; a service
    that exits, goes unhealthy, or becomes unrecognizable has gone somewhere
    it will not come back from on its own, and the services depending on it
    should not spend their budgets finding that out one at a time.
    """

    if item.readiness == "stopped":
        return f"{item.service} had become ready, then exited with code {item.exit_code}."
    if item.readiness == "unhealthy":
        return f"{item.service} had become ready and is now unhealthy."
    if item.readiness == "unknown":
        return f"{item.service} had become ready and is now in a state DevDock does not model."
    return None


def _blocking(graph: DependencyGraph, service: str, tracker: _Tracker) -> tuple[Dependency, ...]:
    """Required dependencies of ``service`` that do not yet meet their condition.

    An optional dependency never blocks. Compose only warns when one is
    unavailable, so waiting on it would be stricter than the project asked.
    """

    return tuple(
        edge
        for edge in graph.dependencies_of(service)
        if edge.required and not condition_met(edge.condition, tracker.for_service(edge.depends_on))
    )


def wait_for_ready(
    graph: DependencyGraph,
    observe: Observer,
    *,
    clock: Clock | None = None,
    cancel: CancelToken | None = None,
    ready_timeout: float = DEFAULT_READY_TIMEOUT,
    deadline: float = DEFAULT_DEADLINE,
    require_ready: bool = False,
) -> WaitOutcome:
    """Poll until every service has settled, or until something says to stop.

    A service's own budget starts when it is running *and* its dependencies
    are satisfied, not when the wait began. Without that rule a service deep
    in the graph is charged for the time its ancestors took and times out for
    someone else's slowness.
    """

    ticker = RealClock() if clock is None else clock
    token = CancelToken() if cancel is None else cancel
    tracker = _Tracker()
    started = ticker.now()
    interval = INITIAL_POLL_INTERVAL
    errors = 0
    last_error: str | None = None

    for name in graph.nodes:
        tracker.for_service(name)

    while True:
        if token.cancelled:
            return _outcome(
                "cancelled",
                "Interrupted while waiting for services to become ready.",
                (
                    "Nothing was stopped or removed. Run 'devdock status' to see where the "
                    "project got to, or 'devdock down' to stop it.",
                ),
                tracker=tracker,
                elapsed=ticker.now() - started,
            )

        snapshot = observe()
        now = ticker.now() - started
        if snapshot.error is not None:
            errors += 1
            last_error = snapshot.error
            if errors >= MAX_CONSECUTIVE_ERRORS:
                return _outcome(
                    "unobservable",
                    "Gave up waiting: the runtime stopped answering.",
                    (f"The last {errors} status queries failed: {last_error}",),
                    tracker=tracker,
                    elapsed=now,
                )
        else:
            errors = 0
            observed = {status.service: status for status in snapshot.services}
            for name in graph.nodes:
                item = tracker.for_service(name)
                status = observed.get(name)
                if status is not None:
                    _apply(item, status, now, require_ready)

            settled = _evaluate(graph, tracker, now, require_ready)
            if settled is not None:
                return _outcome(*settled, tracker=tracker, elapsed=now)

        expired = _expired(graph, tracker, now, ready_timeout, deadline)
        if expired is not None:
            return _outcome(*expired, tracker=tracker, elapsed=now)

        ticker.sleep(interval)
        interval = min(interval * 2, MAX_POLL_INTERVAL)


def _evaluate(
    graph: DependencyGraph, tracker: _Tracker, now: float, require_ready: bool
) -> tuple[WaitStatus, str, tuple[str, ...]] | None:
    """Update blocking and timers, then report a settled result or ``None``.

    Checks run in order of how much they explain. A regression or an
    impossible condition names a cause; a failure names a service; running out
    of time names only the symptom, so it is left to the caller's deadline
    check after this one has found nothing better.
    """

    regressions: list[str] = []
    failures: list[str] = []
    impossible: list[str] = []

    for name in graph.nodes:
        item = tracker.for_service(name)
        item.blocked_by = _blocking(graph, name, tracker)

        # The service's own clock starts the moment nothing is holding it
        # back, whether or not its container exists yet. Requiring it to be
        # running instead would leave a service that never appears at all with
        # no budget to exceed, so it would sit until the global deadline and
        # then be reported as the whole project running late rather than as
        # the one service that never arrived. Cleared again if a dependency
        # stops being satisfied, so nothing is charged for a relapse upstream.
        if item.blocked_by:
            item.unblocked_at = None
        elif item.unblocked_at is None:
            item.unblocked_at = now

        for edge in item.blocked_by:
            reason = condition_unsatisfiable(
                edge.condition, tracker.for_service(edge.depends_on), now
            )
            if reason is not None:
                impossible.append(f"{name} cannot start: {reason}")

        if _settled(item, require_ready):
            if not item.reached_success:
                item.reached_success = True
                item.succeeded_at = now
            continue

        regression = _regression(item) if item.reached_success else None
        if regression is not None:
            regressions.append(regression)
            continue

        failure = _failure(item)
        if failure is not None and not item.reached_success:
            failures.append(failure)

    if regressions:
        return (
            "regressed",
            "A service that had become ready did not stay that way.",
            tuple(regressions)
            + ("The project was left running. Run 'devdock status' to see its current state.",),
        )
    if impossible:
        return (
            "unsatisfiable",
            "Refusing to keep waiting: a declared condition can never be met.",
            tuple(impossible),
        )
    if failures:
        return (
            "failed",
            "A service failed before it became ready.",
            tuple(failures)
            + ("Inspect it with 'docker compose logs', correct the service, then retry.",),
        )

    pending = [
        item.service
        for item in (tracker.for_service(name) for name in graph.nodes)
        if not _settled(item, require_ready)
    ]
    if pending:
        return None
    unverified = [
        item.service
        for item in (tracker.for_service(name) for name in graph.nodes)
        if not item.verified and item.readiness == _SUCCESS_WITHOUT_SOURCES
    ]
    if unverified:
        return (
            "unverified",
            "All services are running, but not all of them could be verified.",
            (
                f"{len(unverified)} service(s) have no readiness source, so DevDock can show "
                "they are running but not that they are serving: " + ", ".join(sorted(unverified)),
                "Add a healthcheck to the Compose file, or a readiness probe under services "
                "in devdock.yaml.",
            ),
        )
    return ("ready", "All services are ready.", ())


def _expired(
    graph: DependencyGraph,
    tracker: _Tracker,
    now: float,
    ready_timeout: float,
    deadline: float,
) -> tuple[WaitStatus, str, tuple[str, ...]] | None:
    """Report the global deadline first, then any service past its own budget."""

    if now >= deadline:
        waiting = sorted(
            item.service
            for item in tracker.progress.values()
            if not item.reached_success and item.service in graph.nodes
        )
        return (
            "deadline_exceeded",
            f"The whole startup exceeded its {deadline:g}s deadline.",
            (
                "Still not ready: " + ", ".join(waiting) + ".",
                "The project was left running. Raise --deadline, or run 'devdock status' to "
                "see what it is waiting for.",
            ),
        )

    overdue = sorted(
        (item for item in tracker.progress.values() if _overdue(item, now, ready_timeout)),
        key=lambda item: item.service,
    )
    if not overdue:
        return None
    detail = [
        f"{item.service} has been ready to start for {item.waited(now):.1f}s and is "
        f"{item.readiness}." + _last_result(item)
        for item in overdue
    ]
    names = ", ".join(item.service for item in overdue)
    return (
        "timed_out",
        f"{names} did not become ready within {ready_timeout:g}s.",
        tuple(detail)
        + (
            "The project was left running. Raise --ready-timeout, or inspect the service "
            "with 'docker compose logs'.",
        ),
    )


def _overdue(item: ServiceProgress, now: float, ready_timeout: float) -> bool:
    """Past its budget only if its own clock started and it has not succeeded."""

    return (
        not item.reached_success
        and item.unblocked_at is not None
        and now - item.unblocked_at >= ready_timeout
    )


def _last_result(item: ServiceProgress) -> str:
    failing = [source for source in item.sources if not source.passed]
    if not failing:
        return ""
    return " Last result: " + "; ".join(source.describe() for source in failing) + "."


def _outcome(
    status: WaitStatus,
    summary: str,
    detail: tuple[str, ...],
    *,
    tracker: _Tracker,
    elapsed: float,
) -> WaitOutcome:
    return WaitOutcome(
        status=status,
        elapsed=elapsed,
        progress=tuple(sorted(tracker.progress.values(), key=lambda item: item.service)),
        summary=summary,
        detail=detail,
    )
