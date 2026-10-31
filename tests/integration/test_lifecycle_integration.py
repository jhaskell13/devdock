"""End-to-end lifecycle checks against a real Docker daemon.

These confirm the assumptions the unit suite can only fake: the Compose label
names DevDock treats as proof of ownership, the field names in `compose ps
--format json`, and the runtime phrasing the failure taxonomy matches on.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from helpers import compose_teardown, docker, run_devdock

from devdock.adapters import ComposeAdapter
from devdock.config import load_profile
from devdock.discovery import CONFIG_NAME
from devdock.lifecycle import (
    LifecycleResult,
    StopPlan,
    execute_stop,
    plan_stop,
    project_status,
    start_project,
)
from devdock.ownership import OwnershipScope, assess_ownership, ownership_scope

pytestmark = pytest.mark.docker


def adapter() -> ComposeAdapter:
    return ComposeAdapter()


def profile_at(root: Path):  # type: ignore[no-untyped-def]
    return load_profile(root / CONFIG_NAME)


def status_report(root: Path) -> LifecycleResult:
    result = project_status(profile_at(root), root, adapter=adapter())
    assert result.ok, "\n".join(result.lines)
    return result


def start(root: Path) -> None:
    result = start_project(profile_at(root), root, adapter=adapter(), timeout=180)
    assert result.ok, "\n".join(result.lines)


def stop(root: Path) -> str:
    plan = plan_stop(profile_at(root), root, adapter=adapter())
    assert isinstance(plan, StopPlan), getattr(plan, "lines", plan)
    return execute_stop(plan, adapter=adapter(), timeout=180).code


def test_a_project_can_be_started_queried_and_stopped(project: Path, host_port: int) -> None:
    before = run_devdock(["status"], project)
    assert "missing" in before.stdout

    started = run_devdock(["up"], project)
    assert started.returncode == 0, started.stdout + started.stderr
    # The demo includes a service with nothing to verify it, on purpose, so a
    # fully successful start is reported as verified-where-it-could-be.
    assert "UP_READY_UNVERIFIED" in started.stdout
    assert "worker" in started.stdout

    queried = run_devdock(["status"], project)
    assert queried.returncode == 0, queried.stdout + queried.stderr
    assert "STATUS_REPORTED" in queried.stdout
    assert f"{host_port}->80/tcp" in queried.stdout

    stopped = run_devdock(["down", "--yes"], project)
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert "DOWN_STOPPED" in stopped.stdout


def test_ownership_labels_match_what_devdock_expects(project: Path, scope: OwnershipScope) -> None:
    """The core safety assumption: Compose writes the labels DevDock checks."""

    start(project)

    inventory = adapter().list_project_resources(scope.project_name)
    assessment = assess_ownership(scope, inventory)

    assert assessment.safe_to_act, "\n".join(assessment.explain())
    assert len(assessment.managed) == 3
    assert {item.service for item in assessment.managed} == {"migrate", "web", "worker"}
    for record in assessment.managed:
        assert record.project == scope.project_name
        assert Path(record.working_dir) == scope.root
        assert Path(record.config_files) == scope.compose_file
        assert record.oneoff.lower() == "false"
    assert inventory.networks and inventory.volumes
    assert inventory.errors == ()


def test_status_distinguishes_ready_from_running(project: Path) -> None:
    start(project)

    deadline = time.monotonic() + 60
    text = ""
    while time.monotonic() < deadline:
        text = "\n".join(status_report(project).lines)
        if any(line.startswith("web ") and " ready " in line for line in text.splitlines()):
            break
        time.sleep(1)

    assert any(line.startswith("web ") and " ready " in line for line in text.splitlines()), text
    # worker declares no health check, so DevDock must not claim it is ready
    assert "running (readiness unverified)" in text


def test_repeated_up_and_down_are_idempotent(project: Path, scope: OwnershipScope) -> None:
    start(project)
    first = {item.id for item in adapter().list_project_resources(scope.project_name).containers}

    start(project)
    second = {item.id for item in adapter().list_project_resources(scope.project_name).containers}

    assert first == second and len(first) == 3

    assert stop(project) == "DOWN_STOPPED"
    assert stop(project) == "DOWN_NOTHING_TO_STOP"


def test_down_preserves_volumes(project: Path, scope: OwnershipScope) -> None:
    start(project)
    before = adapter().list_project_resources(scope.project_name).volumes
    assert before

    assert stop(project) == "DOWN_STOPPED"

    after = adapter().list_project_resources(scope.project_name).volumes
    assert after == before, "down must never remove volumes"


def test_an_unrelated_compose_project_is_left_untouched(project: Path, tmp_path: Path) -> None:
    """The acceptance criterion: DevDock never disturbs another tool's project."""

    outsider = tmp_path / "outsider"
    outsider.mkdir()
    (outsider / "compose.yaml").write_text(
        'services:\n  solo:\n    image: busybox:stable\n    command: ["sleep", "600"]\n',
        encoding="utf-8",
    )
    docker(
        "compose",
        "--project-directory",
        str(outsider),
        "--file",
        str(outsider / "compose.yaml"),
        "--project-name",
        "devdock-integration-outsider",
        "up",
        "--detach",
        check=True,
    )
    try:
        start(project)
        assert stop(project) == "DOWN_STOPPED"

        survivors = docker(
            "ps",
            "--filter",
            "label=com.docker.compose.project=devdock-integration-outsider",
            "--format",
            "{{.Names}}",
        )
        assert survivors.stdout.strip(), "DevDock removed an unrelated project's container"
    finally:
        compose_teardown(outsider, "devdock-integration-outsider")


def test_a_foreign_container_under_the_project_name_blocks_action(
    project: Path, scope: OwnershipScope, leftovers: list[str]
) -> None:
    name = f"{scope.project_name}-intruder-1"
    leftovers.append(name)
    docker("run", "--detach", "--name", name, "busybox:stable", "sleep", "600", check=True)

    result = run_devdock(["up"], project)

    assert result.returncode == 1
    assert "UP_OWNERSHIP_CONFLICT" in result.stderr
    assert "Refusing to act" in result.stderr

    still_running = docker("ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}")
    assert still_running.stdout.strip() == name


def test_port_conflict_is_classified_from_real_runtime_output(
    project: Path, tmp_path: Path
) -> None:
    """Validates a failure-taxonomy marker against Docker's actual phrasing.

    The conflict is provoked with a second Compose project competing for the
    same published port, not by binding the port in this process. Only a native
    Linux daemon shares a network namespace with the test runner; Docker
    Desktop publishes ports inside its own VM, so a local bind would not
    collide and the start would legitimately succeed.
    """

    start(project)

    rival = tmp_path / "rival"
    shutil.copytree(project, rival)
    rival_name = ownership_scope(profile_at(rival), rival).project_name
    assert rival_name != ownership_scope(profile_at(project), project).project_name
    try:
        result = start_project(profile_at(rival), rival, adapter=adapter(), timeout=180)
    finally:
        compose_teardown(rival, rival_name)

    detail = f"{result.summary}\n" + "\n".join(result.lines)
    assert result.code == "UP_PORT_CONFLICT", detail
    assert "Runtime output is hidden" in detail


def test_unreachable_registry_is_classified_as_a_pull_failure(project: Path) -> None:
    """A registry that cannot be reached is a pull failure, not a missing image.

    Docker reports this as a Go timeout ("context deadline exceeded"), which is
    phrasing the taxonomy has to match explicitly. Port 1 is never a registry,
    so this needs no network access.
    """

    _only_service(project, "ghost", "localhost:1/devdock-nonexistent:no-such-tag")

    result = start_project(profile_at(project), project, adapter=adapter(), timeout=180)

    detail = f"{result.summary}\n" + "\n".join(result.lines)
    assert not result.ok
    assert result.code == "UP_PULL_FAILED", detail


def test_missing_tag_is_classified_as_image_not_found(project: Path) -> None:
    """A reachable registry that has no such tag is a missing image."""

    _only_service(project, "ghost", "busybox:devdock-no-such-tag-exists")

    result = start_project(profile_at(project), project, adapter=adapter(), timeout=180)

    detail = f"{result.summary}\n" + "\n".join(result.lines)
    assert not result.ok
    if result.code == "UP_PULL_FAILED":
        pytest.skip("The registry was not reachable, so the tag could not be checked.")
    assert result.code == "UP_IMAGE_NOT_FOUND", detail


def test_compose_ps_json_fields_parse_against_the_real_runtime(
    project: Path, scope: OwnershipScope, host_port: int
) -> None:
    """Confirms the field names the status parser relies on."""

    start(project)

    observations = adapter().observe_services(
        project_name=scope.project_name,
        project_root=scope.root,
        compose_file=scope.compose_file,  # type: ignore[arg-type]
    )

    assert observations.error is None
    assert observations.skipped == 0, "runtime reported entries DevDock could not parse"
    assert len(observations.items) == 3
    web = next(item for item in observations.items if item.service == "web")
    assert web.id and web.name and web.state == "running"
    assert any(port.host_port == host_port and port.container_port == 80 for port in web.ports)


def test_devdock_reports_ownership_scope_before_acting(
    project: Path, scope: OwnershipScope
) -> None:
    result = run_devdock(["down", "--dry-run"], project)

    assert result.returncode == 0
    assert f"Compose project name: {scope.project_name}" in result.stdout
    assert "DOWN_DRY_RUN" in result.stdout


def test_check_passes_against_a_real_daemon(project: Path) -> None:
    result = run_devdock(["check"], project)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "COMPOSE_CLI_AVAILABLE" in result.stdout
    assert "DOCKER_DAEMON_AVAILABLE" in result.stdout
    assert "COMPOSE_CONFIG_VALID" in result.stdout


# --- waiting, against the real runtime -----------------------------------


def test_up_waits_and_reports_what_proved_each_service_ready(project: Path) -> None:
    """The whole milestone in one command, against a real daemon.

    The demo project is built so this exercises every readiness class at
    once: a one-shot task that exits cleanly, a service verified by both a
    Compose health check and an HTTP probe against its published port, and one
    with nothing to verify it.
    """

    result = run_devdock(["up"], project)
    assert result.returncode == 0, result.stdout + result.stderr
    text = result.stdout

    # The exact code, not a prefix of it: UP_READY would also match
    # UP_READY_UNVERIFIED, which is what this project actually produces.
    assert "UP_READY_UNVERIFIED" in text
    assert "Startup order" in text
    assert "web depends on migrate (service_completed_successfully" in text
    assert "worker depends on web (service_healthy" in text
    assert "[PASS] compose health reports healthy" in text
    assert "[PASS] http GET 127.0.0.1:" in text
    assert "migrate: exited_ok" in text


def test_a_second_up_is_already_ready(project: Path) -> None:
    assert run_devdock(["up"], project).returncode == 0

    result = run_devdock(["up"], project)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "UP_READY_UNVERIFIED" in result.stdout


def test_wait_reports_ready_without_starting_anything(project: Path, scope: OwnershipScope) -> None:
    assert run_devdock(["up"], project).returncode == 0
    before = {item.id for item in adapter().list_project_resources(scope.project_name).containers}

    result = run_devdock(["wait"], project)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "WAIT_READY_UNVERIFIED" in result.stdout
    after = {item.id for item in adapter().list_project_resources(scope.project_name).containers}
    assert before == after, "wait must not create or replace anything"


def test_wait_times_out_on_a_project_that_was_never_started(project: Path) -> None:
    """Nothing exists, so every service is missing and its own budget runs out."""

    result = run_devdock(["wait", "--ready-timeout", "3", "--deadline", "40"], project)

    assert result.returncode == 1
    assert "WAIT_READINESS_TIMEOUT" in result.stderr


def test_no_wait_returns_before_the_services_are_ready(project: Path) -> None:
    result = run_devdock(["up", "--no-wait"], project)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "UP_STARTED" in result.stdout
    assert "readiness was not verified" in result.stdout


def test_a_cycle_that_only_exists_after_merging_is_refused_before_anything_starts(
    project: Path, scope: OwnershipScope
) -> None:
    """The case only DevDock can catch, and the gate runs before any mutation.

    A cycle written entirely inside the Compose file is rejected by Compose
    itself, with its own message. A cycle that appears only once the profile's
    dependencies are merged with the Compose file's is invisible to both
    sources on their own -- neither is cyclic -- and is what the merged graph
    is for.
    """

    (project / "compose.yaml").write_text(
        "services:\n"
        "  a:\n"
        "    image: busybox:stable\n"
        '    command: ["sh", "-c", "sleep 60"]\n'
        "    depends_on: [b]\n"
        "  b:\n"
        "    image: busybox:stable\n"
        '    command: ["sh", "-c", "sleep 60"]\n',
        encoding="utf-8",
    )
    (project / CONFIG_NAME).write_text(
        "schema_version: 1\n"
        "project: devdock-demo\n"
        "compose_file: compose.yaml\n"
        "services:\n"
        "  b:\n"
        "    depends_on:\n"
        "      a: service_started\n",
        encoding="utf-8",
    )

    result = run_devdock(["up"], project)

    assert result.returncode == 1
    assert "UP_DEPENDENCY_CYCLE" in result.stderr, result.stderr
    assert "a depends on b depends on a" in result.stderr or (
        "b depends on a depends on b" in result.stderr
    )
    inventory = adapter().list_project_resources(scope.project_name)
    assert inventory.containers == (), "nothing may be created when the graph is refused"


def test_a_cycle_inside_the_compose_file_is_caught_by_the_checks(project: Path) -> None:
    """Compose rejects it, and `up` meets that before it ever builds a graph.

    ``up`` runs the profile's own checks first, and those parse the Compose
    file, so a cycle written inside it never reaches DevDock's graph. The
    message is Compose's finding, reported through ``check``.
    """

    _cyclic_compose(project)

    result = run_devdock(["up"], project)

    assert result.returncode == 1
    assert "UP_PREFLIGHT_FAILED" in result.stderr, result.stderr
    assert "COMPOSE_CONFIG_INVALID" in result.stderr


def test_wait_reports_an_unreadable_compose_file_as_such(project: Path) -> None:
    """``wait`` runs no checks, so it is where the config gate itself shows up."""

    _cyclic_compose(project)

    result = run_devdock(["wait"], project)

    assert result.returncode == 1
    assert "WAIT_CONFIG_UNREADABLE" in result.stderr, result.stderr


def _cyclic_compose(project: Path) -> None:
    (project / "compose.yaml").write_text(
        "services:\n"
        '  a: {image: busybox:stable, command: ["true"], depends_on: [b]}\n'
        '  b: {image: busybox:stable, command: ["true"], depends_on: [a]}\n',
        encoding="utf-8",
    )
    _plain_profile(project)


def _only_service(project: Path, name: str, image: str) -> None:
    """Replace the demo's Compose file with a single service, and the profile with it.

    Both have to change together: the profile declares readiness for the
    demo's own services, and DevDock refuses to start a project whose profile
    configures a service the Compose file does not declare.
    """

    (project / "compose.yaml").write_text(
        f"services:\n  {name}:\n    image: {image}\n", encoding="utf-8"
    )
    _plain_profile(project)


def _plain_profile(project: Path) -> None:
    """The demo profile minus the parts tied to the demo's own services.

    ``runtimes: docker: compose`` stays, because it is what makes ``up`` parse
    the Compose file during its checks, and that decides which gate reports a
    broken file. A profile without it skips that check and reaches a different
    one, which would make these tests assert the wrong code for the wrong
    reason.
    """

    (project / CONFIG_NAME).write_text(
        "schema_version: 1\n"
        "project: devdock-demo\n"
        "runtimes:\n"
        "  docker: compose\n"
        "compose_file: compose.yaml\n",
        encoding="utf-8",
    )


# --- staged startup, against the real runtime -----------------------------


def test_a_staged_start_produces_the_same_project_as_a_single_call(
    project: Path, scope: OwnershipScope
) -> None:
    """The riskiest claim of the staged path: it changes how, not what.

    Staging sends one `up` per dependency layer with --no-deps, instead of
    one call for everything. Compose has to end up in the same place either
    way, or DevDock has quietly changed what starting a project means.
    """

    staged = run_devdock(["up"], project)
    assert staged.returncode == 0, staged.stdout + staged.stderr
    by_service = {
        item.service: item
        for item in adapter().list_project_resources(scope.project_name).containers
    }
    assert set(by_service) == {"migrate", "web", "worker"}

    assert run_devdock(["down", "--yes"], project).returncode == 0

    single = run_devdock(["up", "--no-stage"], project)
    assert single.returncode == 0, single.stdout + single.stderr
    after = {
        item.service: item
        for item in adapter().list_project_resources(scope.project_name).containers
    }

    assert set(after) == set(by_service)
    for service, record in after.items():
        assert record.project == by_service[service].project
        assert record.config_files == by_service[service].config_files


def test_a_staged_start_respects_the_declared_order(project: Path, scope: OwnershipScope) -> None:
    """Containers are created layer by layer, which the daemon records."""

    assert run_devdock(["up"], project).returncode == 0

    created = {}
    for record in adapter().list_project_resources(scope.project_name).containers:
        inspected = docker("inspect", "--format", "{{.Created}}", record.id, check=True)
        created[record.service] = inspected.stdout.strip()

    assert created["migrate"] < created["web"] < created["worker"], created


def test_a_parallelism_limit_is_accepted_by_the_runtime(project: Path) -> None:
    """`--parallel` is a global Compose option; the wrong position is a hard error."""

    result = run_devdock(["up", "--max-parallel", "1"], project)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "UP_READY_UNVERIFIED" in result.stdout


def test_no_deps_does_not_strand_a_service_whose_dependency_is_already_up(
    project: Path,
) -> None:
    """A second `up` after a partial one must still complete the project."""

    assert run_devdock(["up", "--no-wait"], project).returncode == 0

    result = run_devdock(["up"], project)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "UP_READY_UNVERIFIED" in result.stdout
