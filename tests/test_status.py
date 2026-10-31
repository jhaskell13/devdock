from pathlib import Path

import pytest

from devdock.adapters import ContainerRecord, PublishedPort, ServiceObservation
from devdock.config import ServiceProfile
from devdock.ownership import Conflict, OwnershipScope, derive_project_name
from devdock.probes import ProbeResult
from devdock.status import (
    ProjectStatus,
    SourceResult,
    build_service_statuses,
    classify_readiness,
    health_source,
)


def source(passed: bool, kind: str = "tcp") -> SourceResult:
    return SourceResult(kind, f"{kind} 127.0.0.1:5432", passed, "accepted a connection")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("state", "health", "readiness"),
    [
        ("running", "healthy", "ready"),
        ("running", "starting", "starting"),
        ("running", "unhealthy", "unhealthy"),
        ("running", "", "running"),
        ("Running", "HEALTHY", "ready"),
        ("exited", "", "stopped"),
        ("exited", "unhealthy", "stopped"),
        ("created", "", "stopped"),
        ("paused", "", "stopped"),
        ("restarting", "", "stopped"),
        ("dead", "", "stopped"),
        ("teleporting", "", "unknown"),
        ("", "", "unknown"),
    ],
)
def test_readiness_classification_from_compose_health(
    state: str, health: str, readiness: str
) -> None:
    sources = [item for item in (health_source(health),) if item is not None]

    assert classify_readiness(state, health, sources=sources) == readiness


def test_running_without_any_source_is_never_called_ready() -> None:
    assert classify_readiness("running", "") == "running"


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        ([source(True)], "ready"),
        ([source(False)], "starting"),
        ([source(True), source(True, "http")], "ready"),
        ([source(True), source(False, "http")], "starting"),
        ([source(False), source(False, "http")], "starting"),
    ],
)
def test_every_source_must_agree_before_a_service_is_ready(
    sources: list[SourceResult], expected: str
) -> None:
    assert classify_readiness("running", "", sources=sources) == expected


def test_a_failing_probe_says_starting_rather_than_unhealthy() -> None:
    """One sample is not evidence of a broken service, and a probe has no retries.

    A Compose health check does, which Docker has already exhausted before it
    says unhealthy, so that one is taken at its word.
    """

    assert classify_readiness("running", "", sources=[source(False)]) == "starting"
    assert classify_readiness("running", "unhealthy") == "unhealthy"


def test_a_failing_compose_health_check_outranks_passing_probes() -> None:
    passing = health_source("unhealthy")
    assert passing is not None

    assert classify_readiness("running", "unhealthy", sources=[passing, source(True)]) == (
        "unhealthy"
    )


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [(0, "exited_ok"), (1, "stopped"), (137, "stopped"), (None, "stopped")],
)
def test_an_exit_code_of_zero_is_distinguished_from_a_failure(
    exit_code: int | None, expected: str
) -> None:
    assert classify_readiness("exited", "", exit_code=exit_code) == expected


def test_only_an_exited_container_can_be_reported_as_completed() -> None:
    """A running container with no exit code is not retroactively 'completed'."""

    assert classify_readiness("created", "", exit_code=0) == "stopped"
    assert classify_readiness("running", "", exit_code=0) == "running"


@pytest.mark.parametrize(
    ("health", "expected"),
    [("", None), ("   ", None), ("healthy", True), ("HEALTHY", True), ("starting", False)],
)
def test_absent_compose_health_is_not_a_source_rather_than_a_failed_one(
    health: str, expected: bool | None
) -> None:
    result = health_source(health)

    assert (None if result is None else result.passed) == expected


def scope(tmp_path: Path) -> OwnershipScope:
    root = tmp_path.resolve()
    return OwnershipScope(derive_project_name("sample", root), root, root / "compose.yaml")


def record(
    name: str, service: str, state: str = "running", identifier: str = ""
) -> ContainerRecord:
    return ContainerRecord(
        id=identifier or f"id-{name}",
        name=name,
        state=state,
        project="p",
        working_dir="/p",
        config_files="/p/compose.yaml",
        service=service,
        oneoff="False",
    )


def observation(
    name: str,
    service: str,
    state: str = "running",
    health: str = "",
    exit_code: int | None = None,
    ports: tuple[PublishedPort, ...] = (),
    identifier: str = "",
) -> ServiceObservation:
    return ServiceObservation(
        id=identifier or f"id-{name}",
        name=name,
        service=service,
        state=state,
        health=health,
        exit_code=exit_code,
        ports=ports,
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
    assert "cannot tell whether they are serving" in text
    assert report.complete and not report.all_ready


def test_all_ready_requires_every_declared_service(tmp_path: Path) -> None:
    statuses, _ = build_service_statuses(
        ("web",), (record("p-web-1", "web"),), (observation("p-web-1", "web", health="healthy"),)
    )

    assert ProjectStatus(scope(tmp_path), statuses, (), ()).all_ready
    assert not ProjectStatus(scope(tmp_path), (), (), ()).all_ready


# --- probes in the status report ----------------------------------------


def seen(
    service: str,
    *,
    state: str = "running",
    health: str = "",
    ports: tuple[PublishedPort, ...] = (),
    exit_code: int | None = None,
) -> ServiceObservation:
    return ServiceObservation(
        id=f"id-{service}",
        name=f"c-{service}",
        service=service,
        state=state,
        health=health,
        exit_code=exit_code,
        ports=ports,
    )


def profile_with(*probes: object) -> ServiceProfile:
    return ServiceProfile.model_validate({"readiness": list(probes)})


TCP_5432 = {"type": "tcp", "port": 5432}
HTTP_8080 = {"type": "http", "port": 8080, "path": "/healthz"}


class Prober:
    """Answers each probe from a scripted list and records what it was asked."""

    def __init__(self, *results: ProbeResult) -> None:
        self._results = list(results)
        self.asked: list[int] = []

    def __call__(self, probe: object) -> ProbeResult:
        self.asked.append(probe.port)  # type: ignore[attr-defined]
        return self._results.pop(0) if self._results else ProbeResult(True, "accepted a connection")


def test_a_passing_probe_makes_a_service_ready_without_a_health_check() -> None:
    prober = Prober(ProbeResult(True, "answered 200"))

    services, _ = build_service_statuses(
        ("web",),
        (record("c-web", "web"),),
        (seen("web"),),
        profiles={"web": profile_with(HTTP_8080)},
        probe=prober,
    )

    assert services[0].readiness == "ready"
    assert services[0].sources[0].describe() == "http GET 127.0.0.1:8080/healthz answered 200"


def test_a_failing_probe_holds_a_service_at_starting() -> None:
    prober = Prober(ProbeResult(False, "refused the connection"))

    services, _ = build_service_statuses(
        ("db",),
        (record("c-db", "db"),),
        (seen("db"),),
        profiles={"db": profile_with(TCP_5432)},
        probe=prober,
    )

    assert services[0].readiness == "starting"
    assert services[0].failing_sources[0].detail == "refused the connection"


def test_compose_health_and_probes_appear_as_separate_sources() -> None:
    services, _ = build_service_statuses(
        ("web",),
        (record("c-web", "web"),),
        (seen("web", health="healthy"),),
        profiles={"web": profile_with(HTTP_8080)},
        probe=Prober(ProbeResult(True, "answered 200")),
    )

    assert [item.source for item in services[0].sources] == ["compose_health", "http"]
    assert services[0].readiness == "ready"


def test_a_healthy_container_whose_probe_fails_is_not_ready() -> None:
    """The health check tests the container; the probe tests the published port."""

    services, _ = build_service_statuses(
        ("web",),
        (record("c-web", "web"),),
        (seen("web", health="healthy"),),
        profiles={"web": profile_with(HTTP_8080)},
        probe=Prober(ProbeResult(False, "refused the connection")),
    )

    assert services[0].readiness == "starting"


def test_a_service_that_is_not_running_is_never_probed() -> None:
    """Its port is free, so anything answering there belongs to something else."""

    prober = Prober()

    services, _ = build_service_statuses(
        ("db",),
        (record("c-db", "db", state="exited"),),
        (seen("db", state="exited", exit_code=1),),
        profiles={"db": profile_with(TCP_5432)},
        probe=prober,
    )

    assert prober.asked == []
    assert services[0].readiness == "stopped"


def test_a_missing_service_is_never_probed() -> None:
    prober = Prober()

    services, _ = build_service_statuses(
        ("db",), (), (), profiles={"db": profile_with(TCP_5432)}, probe=prober
    )

    assert prober.asked == []
    assert (services[0].readiness, services[0].sources) == ("missing", ())


def test_a_completed_one_shot_task_is_reported_as_such() -> None:
    services, _ = build_service_statuses(
        ("migrate",),
        (record("c-migrate", "migrate", state="exited"),),
        (seen("migrate", state="exited", exit_code=0),),
    )

    assert services[0].readiness == "exited_ok"
    assert services[0].readiness_label == "exited cleanly"
    assert services[0].detail == "exited with code 0"


def test_every_declared_probe_is_run_and_recorded() -> None:
    prober = Prober(ProbeResult(True, "accepted a connection"), ProbeResult(True, "answered 200"))

    services, _ = build_service_statuses(
        ("web",),
        (record("c-web", "web"),),
        (seen("web"),),
        profiles={"web": profile_with(TCP_5432, HTTP_8080)},
        probe=prober,
    )

    assert prober.asked == [5432, 8080]
    assert len(services[0].sources) == 2


def test_a_probe_on_a_port_the_service_does_not_publish_is_called_out() -> None:
    """Using the container's port instead of the published one is the common mistake."""

    _, notes = build_service_statuses(
        ("web",),
        (record("c-web", "web"),),
        (seen("web", ports=(PublishedPort("127.0.0.1", 18080, 80, "tcp"),)),),
        profiles={"web": profile_with({"type": "tcp", "port": 80})},
        probe=Prober(ProbeResult(False, "refused the connection")),
    )

    assert any("does not publish" in note and "18080" in note for note in notes)


def test_a_passing_probe_on_an_unpublished_port_is_not_second_guessed() -> None:
    """Host networking and other arrangements make a port reachable without publishing it."""

    _, notes = build_service_statuses(
        ("web",),
        (record("c-web", "web"),),
        (seen("web", ports=(PublishedPort("127.0.0.1", 18080, 80, "tcp"),)),),
        profiles={"web": profile_with({"type": "tcp", "port": 80})},
        probe=Prober(ProbeResult(True, "accepted a connection")),
    )

    assert notes == ()


def test_a_service_with_no_profile_entry_is_not_probed() -> None:
    prober = Prober()

    build_service_statuses(
        ("web",), (record("c-web", "web"),), (seen("web"),), profiles={}, probe=prober
    )

    assert prober.asked == []


def test_the_report_lists_every_source_with_its_result() -> None:
    services, _ = build_service_statuses(
        ("web",),
        (record("c-web", "web"),),
        (seen("web", health="starting"),),
        profiles={"web": profile_with(HTTP_8080)},
        probe=Prober(ProbeResult(False, "refused the connection")),
    )
    report = ProjectStatus(scope=scope(Path(".")), services=services, conflicts=(), errors=())

    text = "\n".join(report.explain())

    assert "  web: [FAIL] compose health reports starting" in text
    assert "  web: [FAIL] http GET 127.0.0.1:8080/healthz refused the connection" in text


def test_the_report_shows_what_passed_too_not_only_what_failed() -> None:
    services, _ = build_service_statuses(
        ("web",),
        (record("c-web", "web"),),
        (seen("web"),),
        profiles={"web": profile_with(HTTP_8080)},
        probe=Prober(ProbeResult(True, "answered 200")),
    )
    report = ProjectStatus(scope=scope(Path(".")), services=services, conflicts=(), errors=())

    assert "  web: [PASS] http GET 127.0.0.1:8080/healthz answered 200" in "\n".join(
        report.explain()
    )


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("healthy", "reports healthy"),
        ("unhealthy", "reports unhealthy"),
        ("starting", "reports starting"),
        ("\x1b[2Jwiped", "reported a health value DevDock does not recognize"),
        ("something-new", "reported a health value DevDock does not recognize"),
    ],
)
def test_the_health_detail_is_classified_rather_than_echoed(reported: str, expected: str) -> None:
    """It is the one source whose value comes from the runtime, not from DevDock."""

    result = health_source(reported)

    assert result is not None and result.detail == expected
