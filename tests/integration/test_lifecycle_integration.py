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
    assert "UP_STARTED" in started.stdout

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
    assert len(assessment.managed) == 2
    assert {item.service for item in assessment.managed} == {"web", "worker"}
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

    assert first == second and len(first) == 2

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

    (project / "compose.yaml").write_text(
        "services:\n  ghost:\n    image: localhost:1/devdock-nonexistent:no-such-tag\n",
        encoding="utf-8",
    )

    result = start_project(profile_at(project), project, adapter=adapter(), timeout=180)

    detail = f"{result.summary}\n" + "\n".join(result.lines)
    assert not result.ok
    assert result.code == "UP_PULL_FAILED", detail


def test_missing_tag_is_classified_as_image_not_found(project: Path) -> None:
    """A reachable registry that has no such tag is a missing image."""

    (project / "compose.yaml").write_text(
        "services:\n  ghost:\n    image: busybox:devdock-no-such-tag-exists\n",
        encoding="utf-8",
    )

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
    assert len(observations.items) == 2
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
