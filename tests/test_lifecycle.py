import socket
from pathlib import Path

import pytest

from devdock.adapters import (
    ComposeCapabilities,
    ContainerRecord,
    DeclaredServices,
    NetworkRecord,
    OperationOutcome,
    PublishedPort,
    ResourceInventory,
    ServiceObservation,
    ServiceObservations,
)
from devdock.config import ProjectProfile
from devdock.lifecycle import (
    LifecycleResult,
    StopPlan,
    execute_stop,
    plan_stop,
    project_status,
    start_project,
)
from devdock.ownership import derive_project_name, ownership_scope

READY = ComposeCapabilities(
    executable="docker", compose_cli="available", compose_cli_returncode=0, daemon="available"
)


class FakeAdapter:
    """Records lifecycle calls and returns scripted runtime results."""

    def __init__(
        self,
        *,
        capabilities: ComposeCapabilities = READY,
        inventory: ResourceInventory | None = None,
        outcome: OperationOutcome | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._inventory = inventory
        self._outcome = outcome or OperationOutcome("succeeded", 0, "Container web Started", "")
        self.starts: list[dict[str, object]] = []

    def detect_capabilities(self) -> ComposeCapabilities:
        return self._capabilities

    def validate_config(self, config_path: Path, *, project_root: Path):  # type: ignore[no-untyped-def]
        from devdock.adapters import ConfigValidation

        return ConfigValidation(config_path, "valid", 0)

    def list_project_resources(self, project_name: str) -> ResourceInventory:
        return self._inventory or ResourceInventory(project_name)

    def start(
        self, *, project_name: str, project_root: Path, compose_file: Path, timeout: float
    ) -> OperationOutcome:
        self.starts.append(
            {
                "project_name": project_name,
                "project_root": project_root,
                "compose_file": compose_file,
                "timeout": timeout,
            }
        )
        return self._outcome


def make_project(tmp_path: Path, compose: str = "services:\n  web:\n    image: x\n") -> Path:
    (tmp_path / "compose.yaml").write_text(compose, encoding="utf-8")
    return tmp_path


def profile(**overrides: object) -> ProjectProfile:
    fields: dict[str, object] = {"schema_version": 1, "project": "sample"}
    fields.update(overrides)
    return ProjectProfile(**fields)  # type: ignore[arg-type]


# --- happy path ------------------------------------------------------------


def test_successful_start_passes_the_derived_scope_to_the_adapter(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    adapter = FakeAdapter()

    result = start_project(profile(), root, adapter=adapter, environ={}, timeout=42)

    assert result.ok and result.code == "UP_STARTED"
    call = adapter.starts[0]
    assert call["project_name"] == derive_project_name("sample", root)
    assert call["project_root"] == root.resolve()
    assert call["compose_file"] == root.resolve() / "compose.yaml"
    assert call["timeout"] == 42


def test_success_states_that_readiness_was_not_verified(tmp_path: Path) -> None:
    result = start_project(profile(), make_project(tmp_path), adapter=FakeAdapter(), environ={})

    assert any("readiness was not verified" in line for line in result.lines)


def test_ownership_preview_is_printed_before_acting(tmp_path: Path) -> None:
    root = make_project(tmp_path)

    result = start_project(profile(), root, adapter=FakeAdapter(), environ={})

    text = "\n".join(result.lines)
    assert f"Compose project name: {derive_project_name('sample', root)}" in text
    assert f"Project root: {root.resolve()}" in text


def test_repeated_start_is_idempotent_when_resources_are_already_ours(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    running = ContainerRecord(
        id="c1",
        name=f"{scope.project_name}-web-1",
        state="running",
        project=scope.project_name,
        working_dir=str(scope.root),
        config_files=str(scope.compose_file),
        service="web",
        oneoff="False",
    )
    adapter = FakeAdapter(inventory=ResourceInventory(scope.project_name, containers=(running,)))

    first = start_project(profile(), root, adapter=adapter, environ={})
    second = start_project(profile(), root, adapter=adapter, environ={})

    assert first.ok and second.ok
    assert len(adapter.starts) == 2
    assert adapter.starts[0] == adapter.starts[1]


# --- gates that must run before any mutation -------------------------------


def test_preflight_failure_blocks_the_start(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    adapter = FakeAdapter()

    result = start_project(
        profile(required_env=["ABSENT_VARIABLE"]), root, adapter=adapter, environ={}
    )

    assert not result.ok and result.code == "UP_PREFLIGHT_FAILED"
    assert adapter.starts == []
    assert any("ENVIRONMENT_VARIABLE_UNSET" in line for line in result.lines)


def test_missing_compose_file_blocks_the_start(tmp_path: Path) -> None:
    adapter = FakeAdapter()

    result = start_project(profile(), tmp_path, adapter=adapter, environ={})

    assert result.code == "UP_COMPOSE_FILE_MISSING"
    assert adapter.starts == []


def test_compose_file_escaping_the_project_blocks_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(tmp_path)
    adapter = FakeAdapter()
    outside = tmp_path.parent / "external.yaml"
    target = root.resolve() / "compose.yaml"
    original = Path.resolve

    def escape(path: Path, *args: object, **kwargs: object) -> Path:
        if path == target:
            return outside
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", escape)

    result = start_project(profile(), root, adapter=adapter, environ={})

    # Preflight diagnostics catch this first; the dedicated gate remains as defense in depth.
    assert result.code == "UP_PREFLIGHT_FAILED"
    assert any("COMPOSE_FILE_OUTSIDE_PROJECT" in line for line in result.lines)
    assert adapter.starts == []


@pytest.mark.parametrize(
    ("capabilities", "expected"),
    [
        (ComposeCapabilities(executable=None, compose_cli="missing"), "UP_RUNTIME_UNAVAILABLE"),
        (
            ComposeCapabilities(executable="docker", compose_cli="available", daemon="unavailable"),
            "UP_DAEMON_UNAVAILABLE",
        ),
    ],
)
def test_runtime_problems_block_the_start(
    tmp_path: Path, capabilities: ComposeCapabilities, expected: str
) -> None:
    adapter = FakeAdapter(capabilities=capabilities)

    result = start_project(profile(), make_project(tmp_path), adapter=adapter, environ={})

    assert result.code == expected
    assert adapter.starts == []


def test_ownership_conflict_blocks_the_start_and_explains_why(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    intruder = ContainerRecord(id="x1", name=f"{scope.project_name}-web-1", state="running")
    adapter = FakeAdapter(inventory=ResourceInventory(scope.project_name, containers=(intruder,)))

    result = start_project(profile(), root, adapter=adapter, environ={})

    assert result.code == "UP_OWNERSHIP_CONFLICT"
    assert adapter.starts == []
    assert any("Refusing to act" in line for line in result.lines)


def test_incomplete_inventory_blocks_the_start(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    adapter = FakeAdapter(
        inventory=ResourceInventory(scope.project_name, errors=("Network query failed.",))
    )

    result = start_project(profile(), root, adapter=adapter, environ={})

    assert result.code == "UP_OWNERSHIP_CONFLICT"
    assert adapter.starts == []


# --- runtime failures ------------------------------------------------------


def test_failed_start_summarizes_stderr_and_redacts_it(tmp_path: Path) -> None:
    adapter = FakeAdapter(
        outcome=OperationOutcome("failed", 1, "", "bind failed for hunter2-very-secret")
    )

    result = start_project(
        profile(),
        make_project(tmp_path),
        adapter=adapter,
        environ={"DB_PASSWORD": "hunter2-very-secret"},
    )

    text = "\n".join(result.lines)
    assert result.code == "UP_FAILED"  # unrecognized, so the tail is still shown
    assert "Runtime exit code: 1 (not recognized by DevDock)." in text
    assert "hunter2-very-secret" not in text
    assert "${DB_PASSWORD}" in text


def test_timeout_warns_that_containers_may_exist(tmp_path: Path) -> None:
    adapter = FakeAdapter(outcome=OperationOutcome("timed_out", None, "pulling", ""))

    result = start_project(
        profile(), make_project(tmp_path), adapter=adapter, environ={}, timeout=5
    )

    assert result.code == "UP_TIMED_OUT"
    assert "5 seconds" in result.summary
    assert any("may have been created" in line for line in result.lines)


def test_unavailable_runtime_during_start_is_reported(tmp_path: Path) -> None:
    adapter = FakeAdapter(outcome=OperationOutcome("unavailable"))

    result = start_project(profile(), make_project(tmp_path), adapter=adapter, environ={})

    assert result.code == "UP_RUNTIME_UNAVAILABLE"


# --- output control --------------------------------------------------------


def test_default_output_is_a_bounded_tail(tmp_path: Path) -> None:
    noisy = "\n".join(f"line {index}" for index in range(60))
    adapter = FakeAdapter(outcome=OperationOutcome("failed", 1, "", noisy))

    result = start_project(profile(), make_project(tmp_path), adapter=adapter, environ={})

    assert any("earlier line(s) hidden" in line for line in result.lines)
    assert not any("Runtime stdout:" in line for line in result.lines)


def test_show_output_includes_both_streams_and_still_redacts(tmp_path: Path) -> None:
    adapter = FakeAdapter(
        outcome=OperationOutcome("succeeded", 0, "pulled image-layer-token", "warning text")
    )

    result = start_project(
        profile(),
        make_project(tmp_path),
        adapter=adapter,
        environ={"TOKEN": "image-layer-token"},
        show_output=True,
    )

    text = "\n".join(result.lines)
    assert "Runtime stdout:" in text and "Runtime stderr:" in text
    assert "image-layer-token" not in text and "${TOKEN}" in text
    assert "cannot be recognized" in text


def test_show_output_cannot_be_used_to_disable_redaction(tmp_path: Path) -> None:
    adapter = FakeAdapter(outcome=OperationOutcome("failed", 1, "", "secret-value-here"))

    shown = start_project(
        profile(),
        make_project(tmp_path),
        adapter=adapter,
        environ={"PW": "secret-value-here"},
        show_output=True,
    )

    assert "secret-value-here" not in "\n".join(shown.lines)


# --- status ----------------------------------------------------------------


DEFAULT_DECLARED = DeclaredServices(("web", "db"))
NO_OBSERVATIONS = ServiceObservations()


class StatusAdapter(FakeAdapter):
    def __init__(
        self,
        *,
        declared: DeclaredServices = DEFAULT_DECLARED,
        observed: ServiceObservations = NO_OBSERVATIONS,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._declared = declared
        self._observed = observed
        self.observe_calls: list[str] = []

    def list_declared_services(self, *, project_root: Path, compose_file: Path) -> DeclaredServices:
        return self._declared

    def observe_services(
        self, *, project_name: str, project_root: Path, compose_file: Path
    ) -> ServiceObservations:
        self.observe_calls.append(project_name)
        return self._observed


def managed(  # type: ignore[no-untyped-def]
    scope, name: str, service: str, state: str = "running", identifier: str = ""
) -> ContainerRecord:
    return ContainerRecord(
        id=identifier or f"id-{name}",
        name=name,
        state=state,
        project=scope.project_name,
        working_dir=str(scope.root),
        config_files=str(scope.compose_file),
        service=service,
        oneoff="False",
    )


def test_status_reports_declared_managed_and_observed_services(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    web = managed(scope, f"{scope.project_name}-web-1", "web")
    adapter = StatusAdapter(
        inventory=ResourceInventory(scope.project_name, containers=(web,)),
        observed=ServiceObservations(
            items=(
                ServiceObservation(
                    web.id,
                    web.name,
                    "web",
                    "running",
                    "healthy",
                    0,
                    (PublishedPort("0.0.0.0", 8080, 80, "tcp"),),
                ),
            )
        ),
    )

    result = project_status(profile(), root, adapter=adapter)
    text = "\n".join(result.lines)

    assert result.ok and result.code == "STATUS_REPORTED"
    assert "0.0.0.0:8080->80/tcp" in text
    assert "Summary: 1 missing, 1 ready." in text
    assert adapter.observe_calls == [scope.project_name]
    assert adapter.starts == []


def test_status_does_not_require_profile_checks_to_pass(tmp_path: Path) -> None:
    result = project_status(
        profile(required_env=["ABSENT_VARIABLE"]), make_project(tmp_path), adapter=StatusAdapter()
    )

    assert result.ok


def test_status_reports_conflicts_instead_of_refusing(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    squatter = ContainerRecord(id="q", name=f"{scope.project_name}-web-1", state="running")
    adapter = StatusAdapter(inventory=ResourceInventory(scope.project_name, containers=(squatter,)))

    result = project_status(profile(), root, adapter=adapter)
    text = "\n".join(result.lines)

    assert result.ok
    assert "Not managed by DevDock (1)" in text
    assert "web" in text and "missing" in text


def test_status_is_incomplete_when_any_query_fails(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    adapter = StatusAdapter(
        inventory=ResourceInventory(scope.project_name, errors=("Volume query failed.",)),
        observed=ServiceObservations(error="Service status query failed with exit code 1."),
    )

    result = project_status(profile(), root, adapter=adapter)
    text = "\n".join(result.lines)

    assert not result.ok and result.code == "STATUS_INCOMPLETE"
    assert "Incomplete: Volume query failed." in text
    assert "Incomplete: Service status query failed" in text
    assert "SERVICE" in text  # partial table is still shown


def test_status_notes_skipped_runtime_entries(tmp_path: Path) -> None:
    adapter = StatusAdapter(observed=ServiceObservations(skipped=2))

    result = project_status(profile(), make_project(tmp_path), adapter=adapter)

    assert result.ok
    assert any("2 runtime status entries could not be parsed" in line for line in result.lines)


def test_status_requires_a_compose_file(tmp_path: Path) -> None:
    result = project_status(profile(), tmp_path, adapter=StatusAdapter())

    assert result.code == "STATUS_COMPOSE_FILE_MISSING"


@pytest.mark.parametrize(
    ("capabilities", "expected"),
    [
        (ComposeCapabilities(executable=None, compose_cli="missing"), "STATUS_RUNTIME_UNAVAILABLE"),
        (
            ComposeCapabilities(executable="docker", compose_cli="available", daemon="unavailable"),
            "STATUS_DAEMON_UNAVAILABLE",
        ),
    ],
)
def test_status_needs_a_responsive_runtime(
    tmp_path: Path, capabilities: ComposeCapabilities, expected: str
) -> None:
    adapter = StatusAdapter(capabilities=capabilities)

    result = project_status(profile(), make_project(tmp_path), adapter=adapter)

    assert result.code == expected and adapter.observe_calls == []


# --- down -------------------------------------------------------------------


class StopAdapter(StatusAdapter):
    """StatusAdapter plus a scripted stop and a second inventory for the post-check."""

    def __init__(
        self,
        *,
        stop_outcome: OperationOutcome | None = None,
        after: ResourceInventory | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._stop_outcome = stop_outcome or OperationOutcome("succeeded", 0, "Removed", "")
        self._after = after
        self.stops: list[dict[str, object]] = []
        self.stale: ResourceInventory | None = None
        self._inventory_calls = 0

    def list_project_resources(self, project_name: str) -> ResourceInventory:
        # Call 1 builds the plan, call 2 re-verifies it before acting, call 3 is the
        # post-stop check. ``stale`` replaces call 2 when set.
        self._inventory_calls += 1
        if self._inventory_calls == 2 and self.stale is not None:
            return self.stale
        if self._inventory_calls > 2 and self._after is not None:
            return self._after
        return super().list_project_resources(project_name)

    def stop(
        self, *, project_name: str, project_root: Path, compose_file: Path, timeout: float
    ) -> OperationOutcome:
        self.stops.append({"project_name": project_name, "timeout": timeout})
        return self._stop_outcome


def running_project(tmp_path: Path):  # type: ignore[no-untyped-def]
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    web = managed(scope, f"{scope.project_name}-web-1", "web")
    db = managed(scope, f"{scope.project_name}-db-1", "db", state="exited")
    old = managed(scope, f"{scope.project_name}-legacy-1", "legacy")
    inventory = ResourceInventory(
        scope.project_name,
        containers=(web, db, old),
        networks=(NetworkRecord("n1", f"{scope.project_name}_default"),),
        volumes=(f"{scope.project_name}_pgdata",),
    )
    return root, scope, inventory


def test_plan_lists_affected_remaining_networks_and_preserved_volumes(tmp_path: Path) -> None:
    root, scope, inventory = running_project(tmp_path)
    adapter = StopAdapter(inventory=inventory)

    plan = plan_stop(profile(), root, adapter=adapter)

    assert isinstance(plan, StopPlan)
    assert [item.service for item in plan.affected] == ["web", "db"]
    assert [item.service for item in plan.remaining] == ["legacy"]
    text = "\n".join(plan.explain())
    assert "Will stop and remove 2 container(s)" in text
    assert "Will remove 1 project network(s)" in text
    assert "no longer declared" in text and "legacy" in text
    assert "Will preserve 1 volume(s)" in text and "_pgdata" in text
    assert adapter.stops == []


def test_plan_does_not_require_profile_checks_to_pass(tmp_path: Path) -> None:
    root, _scope, inventory = running_project(tmp_path)

    plan = plan_stop(
        profile(required_env=["ABSENT_VARIABLE"]), root, adapter=StopAdapter(inventory=inventory)
    )

    assert isinstance(plan, StopPlan)


def test_plan_refuses_on_ownership_conflict(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    squatter = ContainerRecord(id="q", name=f"{scope.project_name}-web-1", state="running")

    plan = plan_stop(
        profile(),
        root,
        adapter=StopAdapter(
            inventory=ResourceInventory(scope.project_name, containers=(squatter,))
        ),
    )

    assert isinstance(plan, LifecycleResult) and plan.code == "DOWN_OWNERSHIP_CONFLICT"


def test_plan_refuses_when_inventory_is_incomplete(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)

    plan = plan_stop(
        profile(),
        root,
        adapter=StopAdapter(inventory=ResourceInventory(scope.project_name, errors=("boom",))),
    )

    assert isinstance(plan, LifecycleResult) and plan.code == "DOWN_OWNERSHIP_CONFLICT"


def test_plan_refuses_when_declared_services_cannot_be_listed(tmp_path: Path) -> None:
    root, _scope, inventory = running_project(tmp_path)
    adapter = StopAdapter(inventory=inventory, declared=DeclaredServices(error="exit code 1"))

    plan = plan_stop(profile(), root, adapter=adapter)

    assert isinstance(plan, LifecycleResult) and plan.code == "DOWN_CONFIG_UNREADABLE"


@pytest.mark.parametrize(
    ("capabilities", "expected"),
    [
        (ComposeCapabilities(executable=None, compose_cli="missing"), "DOWN_RUNTIME_UNAVAILABLE"),
        (
            ComposeCapabilities(executable="docker", compose_cli="available", daemon="unavailable"),
            "DOWN_DAEMON_UNAVAILABLE",
        ),
    ],
)
def test_plan_needs_a_responsive_runtime(
    tmp_path: Path, capabilities: ComposeCapabilities, expected: str
) -> None:
    adapter = StopAdapter(capabilities=capabilities)
    plan = plan_stop(profile(), make_project(tmp_path), adapter=adapter)

    assert isinstance(plan, LifecycleResult) and plan.code == expected


def test_plan_requires_a_compose_file(tmp_path: Path) -> None:
    plan = plan_stop(profile(), tmp_path, adapter=StopAdapter())

    assert isinstance(plan, LifecycleResult) and plan.code == "DOWN_COMPOSE_FILE_MISSING"


def test_execute_stops_then_verifies_and_reports_preserved_volumes(tmp_path: Path) -> None:
    root, scope, inventory = running_project(tmp_path)
    after = ResourceInventory(
        scope.project_name,
        containers=(inventory.containers[2],),  # legacy stays
        volumes=inventory.volumes,
    )
    adapter = StopAdapter(inventory=inventory, after=after)
    plan = plan_stop(profile(), root, adapter=adapter)
    assert isinstance(plan, StopPlan)

    result = execute_stop(plan, adapter=adapter, environ={}, timeout=33)
    text = "\n".join(result.lines)

    assert result.ok and result.code == "DOWN_STOPPED"
    assert adapter.stops == [{"project_name": scope.project_name, "timeout": 33}]
    assert "Preserved 1 volume(s)" in text and "_pgdata" in text
    assert "Left in place as planned: 1 container(s)" in text
    assert "Warning" not in text


def test_execute_reports_partial_when_resources_survive(tmp_path: Path) -> None:
    root, scope, inventory = running_project(tmp_path)
    adapter = StopAdapter(inventory=inventory, after=inventory)  # nothing changed
    plan = plan_stop(profile(), root, adapter=adapter)
    assert isinstance(plan, StopPlan)

    result = execute_stop(plan, adapter=adapter, environ={})
    text = "\n".join(result.lines)

    assert not result.ok and result.code == "DOWN_PARTIAL"
    assert "2 container(s) still exist" in text
    assert "network(s) still exist" in text


def test_execute_with_nothing_to_do_is_idempotent_and_runs_no_command(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    adapter = StopAdapter(
        inventory=ResourceInventory(scope.project_name, volumes=(f"{scope.project_name}_data",))
    )
    plan = plan_stop(profile(), root, adapter=adapter)
    assert isinstance(plan, StopPlan) and plan.nothing_to_do

    first = execute_stop(plan, adapter=adapter, environ={})
    second = execute_stop(plan, adapter=adapter, environ={})

    assert first.ok and first.code == "DOWN_NOTHING_TO_STOP"
    assert second.code == first.code
    assert adapter.stops == []
    assert first.lines == ()  # the CLI already printed the plan
    assert any("Will preserve 1 volume(s)" in line for line in plan.explain())


def test_execute_redacts_runtime_output_on_failure(tmp_path: Path) -> None:
    root, _scope, inventory = running_project(tmp_path)
    adapter = StopAdapter(
        inventory=inventory,
        stop_outcome=OperationOutcome("failed", 1, "", "cannot remove hunter2-very-secret"),
    )
    plan = plan_stop(profile(), root, adapter=adapter)
    assert isinstance(plan, StopPlan)

    result = execute_stop(plan, adapter=adapter, environ={"PW": "hunter2-very-secret"})
    text = "\n".join(result.lines)

    assert result.code == "DOWN_FAILED"
    assert "Runtime exit code: 1 (not recognized by DevDock)." in text
    assert "hunter2-very-secret" not in text and "${PW}" in text


def test_execute_timeout_says_state_may_be_partial(tmp_path: Path) -> None:
    root, _scope, inventory = running_project(tmp_path)
    adapter = StopAdapter(inventory=inventory, stop_outcome=OperationOutcome("timed_out"))
    plan = plan_stop(profile(), root, adapter=adapter)
    assert isinstance(plan, StopPlan)

    result = execute_stop(plan, adapter=adapter, environ={}, timeout=7)

    assert result.code == "DOWN_TIMED_OUT" and "7 seconds" in result.summary
    assert any("may already be stopped" in line for line in result.lines)


def test_execute_refuses_when_resources_changed_after_the_plan(tmp_path: Path) -> None:
    root, scope, inventory = running_project(tmp_path)
    adapter = StopAdapter(inventory=inventory)
    plan = plan_stop(profile(), root, adapter=adapter)
    assert isinstance(plan, StopPlan)
    newcomer = managed(scope, f"{scope.project_name}-cache-1", "cache", identifier="new")
    adapter.stale = ResourceInventory(
        scope.project_name, containers=inventory.containers + (newcomer,)
    )

    result = execute_stop(plan, adapter=adapter, environ={})

    assert not result.ok and result.code == "DOWN_PLAN_STALE"
    assert adapter.stops == []


def test_execute_refuses_when_a_conflict_appears_after_the_plan(tmp_path: Path) -> None:
    root, scope, inventory = running_project(tmp_path)
    adapter = StopAdapter(inventory=inventory)
    plan = plan_stop(profile(), root, adapter=adapter)
    assert isinstance(plan, StopPlan)
    squatter = ContainerRecord(id="sq", name=f"{scope.project_name}-x-1", state="running")
    adapter.stale = ResourceInventory(
        scope.project_name, containers=inventory.containers + (squatter,)
    )

    result = execute_stop(plan, adapter=adapter, environ={})

    assert result.code == "DOWN_PLAN_STALE" and adapter.stops == []


def test_execute_does_not_claim_success_when_verification_fails(tmp_path: Path) -> None:
    root, scope, inventory = running_project(tmp_path)
    adapter = StopAdapter(
        inventory=inventory,
        after=ResourceInventory(scope.project_name, errors=("Container query failed.",)),
    )
    plan = plan_stop(profile(), root, adapter=adapter)
    assert isinstance(plan, StopPlan)

    result = execute_stop(plan, adapter=adapter, environ={})

    assert not result.ok and result.code == "DOWN_PARTIAL"
    assert "could not confirm" in result.summary
    assert any("Could not verify" in line for line in result.lines)


def test_declared_port_already_in_use_does_not_block_a_repeat_start(tmp_path: Path) -> None:
    """A published port is usually held by this project's own containers.

    Treating the probe as blocking would make the second `devdock up` refuse,
    so it is reported as advisory and Compose's own bind stays authoritative.
    """
    root = make_project(tmp_path)
    adapter = FakeAdapter()

    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        result = start_project(profile(ports=[port]), root, adapter=adapter, environ={})

    assert result.ok and result.code == "UP_STARTED"
    assert len(adapter.starts) == 1
    assert any("PORT_UNAVAILABLE" in line for line in result.lines)


def test_other_preflight_failures_still_block_a_start(tmp_path: Path) -> None:
    adapter = FakeAdapter()

    result = start_project(
        profile(required_env=["ABSENT_VARIABLE"]),
        make_project(tmp_path),
        adapter=adapter,
        environ={},
    )

    assert result.code == "UP_PREFLIGHT_FAILED" and adapter.starts == []
