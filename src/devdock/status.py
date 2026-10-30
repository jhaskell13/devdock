"""Per-service status and the readiness model lifecycle commands build on.

``status`` is read-only and reports rather than refuses: ownership conflicts
and incomplete inventories appear in the report instead of blocking it.

A service is ready when *every* readiness source it has agrees. Those sources
are the container's Compose health, when it reports any, and each `tcp` or
`http` probe the profile declares for it. A service with no source at all can
only be shown to be running, which is deliberately not the same as ready.

This module stays pure. It decides which services are worth probing and what
the answers mean; the probe callable it is given does the connecting.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from devdock.adapters import ContainerRecord, PublishedPort, ServiceObservation
from devdock.config import HttpReadiness, ServiceProfile
from devdock.ownership import Conflict, OwnershipScope
from devdock.probes import ProbeResult, ReadinessProbeUnion, describe_target, run_probe

Readiness = Literal[
    "ready",  # running and every readiness source agrees
    "running",  # running with no readiness source: alive, readiness unverified
    "starting",  # running, at least one source has not passed yet
    "unhealthy",  # running, the Compose health check is failing
    "exited_ok",  # exited with code 0, which is what a completed task looks like
    "stopped",  # container exists but is not running
    "missing",  # declared in the Compose file, no managed container
    "unknown",  # the runtime reported a state DevDock does not model
]
ReadinessSource = Literal["compose_health", "tcp", "http"]

ProbeCallable = Callable[[ReadinessProbeUnion], ProbeResult]

RUNNING_STATES = frozenset({"running"})
EXITED_STATE = "exited"
STOPPED_STATES = frozenset({"exited", "created", "paused", "dead", "removing", "restarting"})

READINESS_LABELS: dict[Readiness, str] = {
    "ready": "ready",
    "running": "running (readiness unverified)",
    "starting": "starting",
    "unhealthy": "unhealthy",
    "exited_ok": "exited cleanly",
    "stopped": "stopped",
    "missing": "missing",
    "unknown": "unknown",
}

COMPOSE_HEALTH_TARGET = "compose health"


@dataclass(frozen=True)
class SourceResult:
    """One readiness source's answer about one service.

    ``target`` and ``detail`` are both safe to print: the target is built from
    profile values that rejected whitespace and control characters, and the
    detail comes from a vocabulary fixed in ``probes`` or in this module.
    """

    source: ReadinessSource
    target: str
    passed: bool
    detail: str

    def describe(self) -> str:
        return f"{self.target} {self.detail}"


def health_source(health: str) -> SourceResult | None:
    """Turn a reported Compose health value into a source, or ``None`` if absent.

    An empty value means the container reports no health at all, which is not
    a failure: most images define no health check. It means this source does
    not exist for that service.
    """

    normalized = health.strip().lower()
    if not normalized:
        return None
    return SourceResult(
        "compose_health",
        COMPOSE_HEALTH_TARGET,
        normalized == "healthy",
        f"reports {normalized}",
    )


def probe_source(probe: ReadinessProbeUnion, result: ProbeResult) -> SourceResult:
    kind: ReadinessSource = "http" if isinstance(probe, HttpReadiness) else "tcp"
    return SourceResult(kind, describe_target(probe), result.passed, result.detail)


def classify_readiness(
    state: str,
    health: str,
    *,
    exit_code: int | None = None,
    sources: Sequence[SourceResult] = (),
) -> Readiness:
    """Map a container's observed state and its readiness sources to one class.

    A failing probe yields ``starting``, not ``unhealthy``. One sample is not
    evidence that a service is broken: a probe that fails is exactly what a
    service looks like while it is still coming up, and unlike a Compose
    health check this probe has no retry policy of its own to have exhausted.
    Compose health is the exception, because Docker already applied one before
    calling a container unhealthy.
    """

    normalized_state = state.strip().lower()
    normalized_health = health.strip().lower()
    if normalized_state in RUNNING_STATES:
        if normalized_health == "unhealthy":
            return "unhealthy"
        if not sources:
            return "running"
        return "ready" if all(item.passed for item in sources) else "starting"
    if normalized_state == EXITED_STATE and exit_code == 0:
        return "exited_ok"
    if normalized_state in STOPPED_STATES:
        return "stopped"
    return "unknown"


@dataclass(frozen=True)
class ServiceStatus:
    """What DevDock knows about one declared or observed service container."""

    service: str
    container: str | None
    state: str
    health: str
    exit_code: int | None
    ports: tuple[PublishedPort, ...]
    readiness: Readiness
    sources: tuple[SourceResult, ...]
    declared: bool
    detail: str = ""

    @property
    def readiness_label(self) -> str:
        return READINESS_LABELS[self.readiness]

    @property
    def verified(self) -> bool:
        """Whether anything actually checked this service, rather than assumed it."""

        return bool(self.sources)

    @property
    def failing_sources(self) -> tuple[SourceResult, ...]:
        return tuple(item for item in self.sources if not item.passed)


@dataclass(frozen=True)
class ProjectStatus:
    """A complete status report; ``complete`` is false when any source failed."""

    scope: OwnershipScope
    services: tuple[ServiceStatus, ...]
    conflicts: tuple[Conflict, ...]
    errors: tuple[str, ...]
    notes: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.errors

    @property
    def all_ready(self) -> bool:
        """True only when every declared service is ``ready``; informational for now."""

        declared = [item for item in self.services if item.declared]
        return bool(declared) and all(item.readiness == "ready" for item in declared)

    def counts(self) -> dict[Readiness, int]:
        tally: dict[Readiness, int] = {}
        for item in self.services:
            tally[item.readiness] = tally.get(item.readiness, 0) + 1
        return tally

    def explain(self) -> tuple[str, ...]:
        lines = list(self.scope.describe(boundary=False))
        if self.services:
            lines.extend(_table(self.services))
        else:
            lines.append("Services: none declared and none observed.")
        summary = ", ".join(
            f"{count} {READINESS_LABELS[readiness]}"
            for readiness, count in sorted(self.counts().items(), key=lambda kv: kv[0])
        )
        if summary:
            lines.append(f"Summary: {summary}.")
        unverified = [item.service for item in self.services if item.readiness == "running"]
        if unverified:
            lines.append(
                f"{len(unverified)} service(s) have no readiness source and are shown as "
                "running; DevDock cannot tell whether they are serving. Add a healthcheck "
                "to the Compose file, or a readiness probe under services in devdock.yaml: "
                + ", ".join(sorted(unverified))
            )
        for note in self.notes:
            lines.append(f"Note: {note}")
        if self.conflicts:
            lines.append(
                f"Not managed by DevDock ({len(self.conflicts)}): resources under this "
                "project name that DevDock did not create from this project."
            )
            for conflict in self.conflicts:
                lines.append(f"  {conflict.name} ({conflict.kind})")
                lines.extend(f"    {reason}" for reason in conflict.reasons)
        for error in self.errors:
            lines.append(f"Incomplete: {error}")
        return tuple(lines)


def _table(services: tuple[ServiceStatus, ...]) -> list[str]:
    rows = [
        (
            item.service,
            item.container or "-",
            item.state,
            item.health or "-",
            item.readiness_label,
            ", ".join(port.render() for port in item.ports) or "-",
        )
        for item in services
    ]
    header = ("SERVICE", "CONTAINER", "STATE", "HEALTH", "READINESS", "PORTS")
    widths = [max(len(row[index]) for row in (header, *rows)) for index in range(len(header))]

    def render(cells: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(cells)).rstrip()

    lines = [render(header)]
    lines.extend(render(row) for row in rows)
    for item in services:
        if item.detail:
            lines.append(f"  {item.service}: {item.detail}")
        # Every source is listed, passing or not. A readiness class on its own
        # does not say what was checked, and "ready" is worth no more than the
        # evidence behind it.
        for source in item.sources:
            mark = "PASS" if source.passed else "FAIL"
            lines.append(f"  {item.service}: [{mark}] {source.describe()}")
    return lines


def _evaluate_sources(
    service: str,
    state: str,
    health: str,
    ports: tuple[PublishedPort, ...],
    profiles: Mapping[str, ServiceProfile],
    probe: ProbeCallable,
) -> tuple[tuple[SourceResult, ...], tuple[str, ...]]:
    """Collect every readiness source for one service, probing only if it is running.

    A stopped service is not probed. Its port is free, so whatever answers on
    it belongs to something else, and a probe there would report on a service
    that is not running at all.
    """

    sources: list[SourceResult] = []
    notes: list[str] = []
    reported_health = health_source(health)
    if reported_health is not None:
        sources.append(reported_health)
    if state.strip().lower() not in RUNNING_STATES:
        return tuple(sources), ()

    declared_probes = profiles[service].readiness if service in profiles else ()
    published = {port.host_port for port in ports}
    for item in declared_probes:
        result = probe_source(item, probe(item))
        sources.append(result)
        if not result.passed and published and item.port not in published:
            # The most common way to write this wrong is to use the port
            # inside the container. The probe runs from the host, so it is
            # the published port that it can reach.
            notes.append(
                f"{service} is probed on port {item.port}, which it does not publish; "
                f"it publishes {', '.join(str(value) for value in sorted(published))}."
            )
    return tuple(sources), tuple(notes)


def build_service_statuses(
    declared: tuple[str, ...],
    managed: tuple[ContainerRecord, ...],
    observations: tuple[ServiceObservation, ...],
    *,
    profiles: Mapping[str, ServiceProfile] | None = None,
    probe: ProbeCallable = run_probe,
) -> tuple[tuple[ServiceStatus, ...], tuple[str, ...]]:
    """Join declared services, managed containers, runtime observations, and probes.

    Returns the statuses in declared order followed by managed-but-undeclared
    containers, plus notes about observations DevDock ignored because the
    container was not provably its own.
    """

    service_profiles: Mapping[str, ServiceProfile] = {} if profiles is None else profiles
    by_id = {item.id: item for item in observations}
    by_name = {item.name: item for item in observations}
    managed_ids = {record.id for record in managed}
    managed_names = {record.name for record in managed}
    statuses: list[ServiceStatus] = []
    notes: list[str] = []

    def status_for(record: ContainerRecord, is_declared: bool) -> ServiceStatus:
        observed = by_id.get(record.id) or by_name.get(record.name)
        state = observed.state if observed else record.state.lower()
        health = observed.health if observed else ""
        exit_code = observed.exit_code if observed else None
        ports = observed.ports if observed else ()
        service = record.service or (observed.service if observed else "")
        sources, probe_notes = _evaluate_sources(
            service, state, health, ports, service_profiles, probe
        )
        notes.extend(probe_notes)
        readiness = classify_readiness(state, health, exit_code=exit_code, sources=sources)
        detail = ""
        if readiness in {"stopped", "exited_ok"} and exit_code is not None:
            detail = f"exited with code {exit_code}"
        elif observed is None:
            detail = "no runtime status report; showing inventory state only"
        if not is_declared:
            detail = (detail + "; " if detail else "") + (
                "not declared in the current Compose file"
            )
        return ServiceStatus(
            service=service,
            container=record.name,
            state=state,
            health=health,
            exit_code=exit_code,
            ports=ports,
            readiness=readiness,
            sources=sources,
            declared=is_declared,
            detail=detail,
        )

    seen_records: set[str] = set()
    for name in declared:
        matching = [record for record in managed if record.service == name]
        if not matching:
            statuses.append(
                ServiceStatus(
                    service=name,
                    container=None,
                    state="missing",
                    health="",
                    exit_code=None,
                    ports=(),
                    readiness="missing",
                    sources=(),
                    declared=True,
                    detail="declared but no container exists; run 'devdock up'",
                )
            )
            continue
        for record in sorted(matching, key=lambda item: item.name):
            seen_records.add(record.id)
            statuses.append(status_for(record, True))

    for record in sorted(managed, key=lambda item: item.name):
        if record.id in seen_records:
            continue
        statuses.append(status_for(record, False))

    ignored = [
        item.name
        for item in observations
        if item.id not in managed_ids and item.name not in managed_names
    ]
    if ignored:
        notes.append(
            f"{len(ignored)} runtime-reported container(s) were ignored because DevDock "
            "could not prove it created them: " + ", ".join(sorted(ignored))
        )
    return tuple(statuses), tuple(notes)
