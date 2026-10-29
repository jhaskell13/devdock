"""Per-service status and the readiness model lifecycle commands build on.

``status`` is read-only and reports rather than refuses: ownership conflicts
and incomplete inventories appear in the report instead of blocking it. The
readiness classification here is the vocabulary Milestone 3 will extend with
TCP and HTTP readiness sources and dependency blocking; the Compose health
status is its first and only source today.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from devdock.adapters import ContainerRecord, PublishedPort, ServiceObservation
from devdock.ownership import Conflict, OwnershipScope

Readiness = Literal[
    "ready",  # running and the health check passes
    "running",  # running with no health check: alive, readiness unverified
    "starting",  # running, health check has not yet passed
    "unhealthy",  # running, health check is failing
    "stopped",  # container exists but is not running
    "missing",  # declared in the Compose file, no managed container
    "unknown",  # the runtime reported a state DevDock does not model
]
ReadinessSource = Literal["compose_health", "none"]

RUNNING_STATES = frozenset({"running"})
STOPPED_STATES = frozenset({"exited", "created", "paused", "dead", "removing", "restarting"})

READINESS_LABELS: dict[Readiness, str] = {
    "ready": "ready",
    "running": "running (readiness unverified)",
    "starting": "starting",
    "unhealthy": "unhealthy",
    "stopped": "stopped",
    "missing": "missing",
    "unknown": "unknown",
}


def classify_readiness(state: str, health: str) -> tuple[Readiness, ReadinessSource]:
    """Map a container state and Compose health value to a readiness class.

    ``running`` is deliberately distinct from ``ready``: a container with no
    health check can only be shown to be alive, not to be serving.
    """

    normalized_state = state.strip().lower()
    normalized_health = health.strip().lower()
    if normalized_state in RUNNING_STATES:
        if normalized_health == "healthy":
            return "ready", "compose_health"
        if normalized_health == "starting":
            return "starting", "compose_health"
        if normalized_health == "unhealthy":
            return "unhealthy", "compose_health"
        return "running", "none"
    if normalized_state in STOPPED_STATES:
        return "stopped", "none"
    return "unknown", "none"


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
    readiness_source: ReadinessSource
    declared: bool
    detail: str = ""

    @property
    def readiness_label(self) -> str:
        return READINESS_LABELS[self.readiness]


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
        if any(item.readiness == "running" for item in self.services):
            lines.append(
                "Services without a Compose health check are shown as running; DevDock "
                "cannot tell whether they are ready."
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
    return lines


def build_service_statuses(
    declared: tuple[str, ...],
    managed: tuple[ContainerRecord, ...],
    observations: tuple[ServiceObservation, ...],
) -> tuple[tuple[ServiceStatus, ...], tuple[str, ...]]:
    """Join declared services, managed containers, and runtime observations.

    Returns the statuses in declared order followed by managed-but-undeclared
    containers, plus notes about observations DevDock ignored because the
    container was not provably its own.
    """

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
        readiness, source = classify_readiness(state, health)
        detail = ""
        if readiness == "stopped" and exit_code is not None:
            detail = f"exited with code {exit_code}"
        elif observed is None:
            detail = "no runtime status report; showing inventory state only"
        if not is_declared:
            detail = (detail + "; " if detail else "") + (
                "not declared in the current Compose file"
            )
        return ServiceStatus(
            service=record.service or (observed.service if observed else ""),
            container=record.name,
            state=state,
            health=health,
            exit_code=exit_code,
            ports=ports,
            readiness=readiness,
            readiness_source=source,
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
                    readiness_source="none",
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
