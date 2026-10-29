from pathlib import Path

import pytest

from devdock.adapters import ContainerRecord, PublishedPort, ServiceObservation
from devdock.ownership import Conflict, OwnershipScope, derive_project_name
from devdock.status import (
    ProjectStatus,
    build_service_statuses,
    classify_readiness,
)


@pytest.mark.parametrize(
    ("state", "health", "readiness", "source"),
    [
        ("running", "healthy", "ready", "compose_health"),
        ("running", "starting", "starting", "compose_health"),
        ("running", "unhealthy", "unhealthy", "compose_health"),
        ("running", "", "running", "none"),
        ("Running", "HEALTHY", "ready", "compose_health"),
        ("exited", "", "stopped", "none"),
        ("exited", "unhealthy", "stopped", "none"),
        ("created", "", "stopped", "none"),
        ("paused", "", "stopped", "none"),
        ("restarting", "", "stopped", "none"),
        ("dead", "", "stopped", "none"),
        ("teleporting", "", "unknown", "none"),
        ("", "", "unknown", "none"),
    ],
)
def test_readiness_classification(state: str, health: str, readiness: str, source: str) -> None:
    assert classify_readiness(state, health) == (readiness, source)


def test_running_without_health_check_is_never_called_ready() -> None:
    readiness, _ = classify_readiness("running", "")

    assert readiness == "running" and readiness != "ready"


def scope(tmp_path: Path) -> OwnershipScope:
    root = tmp_path.resolve()
    return OwnershipScope(derive_project_name("sample", root), root, root / "compose.yaml")


def record(
    name: str, service: str, state: str = "running", identifier: str = ""
) -> ContainerRecord:
    return ContainerRecord(
        id=identifier or f"id-{name}", name=name, state=state, project="p", working_dir="/p",
        config_files="/p/compose.yaml", service=service, oneoff="False",
    )


def observation(
    name: str, service: str, state: str = "running", health: str = "", exit_code: int | None = None,
    ports: tuple[PublishedPort, ...] = (), identifier: str = "",
) -> ServiceObservation:
    return ServiceObservation(
        id=identifier or f"id-{name}", name=name, service=service, state=state, health=health,
        exit_code=exit_code, ports=ports,
    )


def test_join_preserves_declared_order_and_marks_missing_services() -> None:
    statuses, notes = build_service_statuses(
        ("web", "db", "cache"),
        (record("p-db-1", "db"), record("p-web-1", "web")),
        (observation("p-web-1", "web", health="healthy"), observation("p-db-1", "db")),
    )

    assert [item.service for item in statuses] == ["web", "db", "cache"]
    assert statuses[0].readiness == "ready"
    assert statuses[1].readiness == "running"
    assert statuses[2].readiness == "missing" and statuses[2].container is None
    assert "devdock up" in statuses[2].detail
    assert notes == ()


def test_replicas_are_listed_individually_in_name_order() -> None:
    statuses, _ = build_service_statuses(
        ("web",),
        (record("p-web-2", "web"), record("p-web-1", "web")),
        (observation("p-web-1", "web"), observation("p-web-2", "web")),
    )

    assert [item.container for item in statuses] == ["p-web-1", "p-web-2"]
    assert all(item.declared for item in statuses)


def test_managed_but_undeclared_container_is_reported_with_a_detail() -> None:
    statuses, _ = build_service_statuses(
        ("web",), (record("p-web-1", "web"), record("p-old-1", "old")), ()
    )

    undeclared = statuses[1]
    assert undeclared.service == "old" and not undeclared.declared
    assert "not declared" in undeclared.detail


def test_observations_for_unmanaged_containers_are_ignored_and_noted() -> None:
    statuses, notes = build_service_statuses(
        ("web",),
        (record("p-web-1", "web"),),
        (observation("p-web-1", "web"), observation("intruder-1", "web", identifier="zzz")),
    )

    assert len(statuses) == 1 and statuses[0].container == "p-web-1"
    assert len(notes) == 1 and "intruder-1" in notes[0]


def test_join_falls_back_to_name_when_ids_differ_in_truncation() -> None:
    statuses, _ = build_service_statuses(
        ("web",),
        (record("p-web-1", "web", identifier="abcdef0123456789" * 4),),
        (observation("p-web-1", "web", health="healthy", identifier="abcdef012345"),),
    )

    assert statuses[0].readiness == "ready"


def test_exit_code_and_missing_observation_are_explained() -> None:
    statuses, _ = build_service_statuses(
        ("db", "web"),
        (record("p-db-1", "db", state="exited"), record("p-web-1", "web", state="running")),
        (observation("p-db-1", "db", state="exited", exit_code=137),),
    )

    assert statuses[0].readiness == "stopped" and "code 137" in statuses[0].detail
    assert statuses[1].readiness == "running"
    assert "inventory state only" in statuses[1].detail


def test_report_renders_table_summary_conflicts_and_errors(tmp_path: Path) -> None:
    ports = (PublishedPort("127.0.0.1", 8080, 80, "tcp"),)
    statuses, _ = build_service_statuses(
        ("web", "db"),
        (record("p-web-1", "web"),),
        (observation("p-web-1", "web", health="healthy", ports=ports),),
    )
    report = ProjectStatus(
        scope=scope(tmp_path),
        services=statuses,
        conflicts=(Conflict("unlabeled", "squatter", ("Missing label.",)),),
        errors=("Network query failed with exit code 1.",),
    )

    text = "\n".join(report.explain())

    assert "SERVICE" in text and "READINESS" in text and "PORTS" in text
    assert "127.0.0.1:8080->80/tcp" in text
    assert "Summary: 1 missing, 1 ready." in text
    assert "Not managed by DevDock (1)" in text and "squatter (unlabeled)" in text
    assert "Incomplete: Network query failed" in text
    assert not report.complete
    assert not report.all_ready


def test_report_explains_running_without_health_check(tmp_path: Path) -> None:
    statuses, _ = build_service_statuses(
        ("web",), (record("p-web-1", "web"),), (observation("p-web-1", "web"),)
    )
    report = ProjectStatus(scope(tmp_path), statuses, (), ())

    text = "\n".join(report.explain())

    assert "running (readiness unverified)" in text
    assert "cannot tell whether they are ready" in text
    assert report.complete and not report.all_ready


def test_all_ready_requires_every_declared_service(tmp_path: Path) -> None:
    statuses, _ = build_service_statuses(
        ("web",), (record("p-web-1", "web"),), (observation("p-web-1", "web", health="healthy"),)
    )

    assert ProjectStatus(scope(tmp_path), statuses, (), ()).all_ready
    assert not ProjectStatus(scope(tmp_path), (), (), ()).all_ready
