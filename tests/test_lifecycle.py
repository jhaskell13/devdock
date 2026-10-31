import socket
from collections.abc import Sequence
from pathlib import Path

import pytest

from devdock.adapters import (
    ComposeCapabilities,
    ContainerRecord,
    DeclaredDependencies,
    DeclaredDependency,
    DeclaredServices,
    NetworkRecord,
    OperationOutcome,
    PublishedPort,
    ResourceInventory,
    ServiceObservation,
    ServiceObservations,
)
from devdock.cancellation import CancelToken
from devdock.config import ProjectProfile
from devdock.lifecycle import (
    LifecycleResult,
    StopPlan,
    execute_stop,
    plan_stop,
    project_status,
    start_project,
    wait_for_project,
)
from devdock.ownership import derive_project_name, ownership_scope
from devdock.probes import ProbeResult

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
        dependencies: DeclaredDependencies | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._inventory = inventory
        self._outcome = outcome or OperationOutcome("succeeded", 0, "Container web Started", "")
        self._dependencies = dependencies
        self.starts: list[dict[str, object]] = []

    def detect_capabilities(self) -> ComposeCapabilities:
        return self._capabilities

    def validate_config(self, config_path: Path, *, project_root: Path):  # type: ignore[no-untyped-def]
        from devdock.adapters import ConfigValidation

        return ConfigValidation(config_path, "valid", 0)

    def list_project_resources(self, project_name: str) -> ResourceInventory:
        return self._inventory or ResourceInventory(project_name)

    def list_declared_services(self, *, project_root: Path, compose_file: Path) -> DeclaredServices:
        return DeclaredServices(names=self._declared_names())

    def list_dependencies(self, *, project_root: Path, compose_file: Path) -> DeclaredDependencies:
        return self._dependencies or DeclaredDependencies(services=self._declared_names())

    def _declared_names(self) -> tuple[str, ...]:
        if self._dependencies is not None and self._dependencies.services:
            return self._dependencies.services
        return ("web",)

    def observe_services(
        self, *, project_name: str, project_root: Path, compose_file: Path
    ) -> ServiceObservations:
        return ServiceObservations()

    def start(
        self,
        *,
        project_name: str,
        project_root: Path,
        compose_file: Path,
        timeout: float,
        cancel: object = None,
        services: Sequence[str] = (),
        no_deps: bool = False,
        max_parallel: int | None = None,
    ) -> OperationOutcome:
        self.starts.append(
            {
                "project_name": project_name,
                "project_root": project_root,
                "compose_file": compose_file,
                "timeout": timeout,
                "services": tuple(services),
                "no_deps": no_deps,
                "max_parallel": max_parallel,
            }
        )
        return self._outcome


def start(*args: object, **kwargs: object) -> LifecycleResult:
    """``start_project`` with waiting off.

    Every test written before waiting existed is about the start itself: what
    is passed to the adapter, what is refused, how output is rendered. Waiting
    is exercised separately, against an injected clock.
    """

    kwargs.setdefault("wait", False)
    return start_project(*args, **kwargs)  # type: ignore[arg-type]


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

    result = start(profile(), root, adapter=adapter, environ={}, timeout=42)

    assert result.ok and result.code == "UP_STARTED"
    call = adapter.starts[0]
    assert call["project_name"] == derive_project_name("sample", root)
    assert call["project_root"] == root.resolve()
    assert call["compose_file"] == root.resolve() / "compose.yaml"
    assert call["timeout"] == 42


def test_success_states_that_readiness_was_not_verified(tmp_path: Path) -> None:
    result = start(profile(), make_project(tmp_path), adapter=FakeAdapter(), environ={})

    assert any("readiness was not verified" in line for line in result.lines)


def test_ownership_preview_is_printed_before_acting(tmp_path: Path) -> None:
    root = make_project(tmp_path)

    result = start(profile(), root, adapter=FakeAdapter(), environ={})

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

    first = start(profile(), root, adapter=adapter, environ={})
    second = start(profile(), root, adapter=adapter, environ={})

    assert first.ok and second.ok
    assert len(adapter.starts) == 2
    assert adapter.starts[0] == adapter.starts[1]


# --- gates that must run before any mutation -------------------------------


def test_preflight_failure_blocks_the_start(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    adapter = FakeAdapter()

    result = start(profile(required_env=["ABSENT_VARIABLE"]), root, adapter=adapter, environ={})

    assert not result.ok and result.code == "UP_PREFLIGHT_FAILED"
    assert adapter.starts == []
    assert any("ENVIRONMENT_VARIABLE_UNSET" in line for line in result.lines)


def test_missing_compose_file_blocks_the_start(tmp_path: Path) -> None:
    adapter = FakeAdapter()

    result = start(profile(), tmp_path, adapter=adapter, environ={})

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

    result = start(profile(), root, adapter=adapter, environ={})

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

    result = start(profile(), make_project(tmp_path), adapter=adapter, environ={})

    assert result.code == expected
    assert adapter.starts == []


def test_ownership_conflict_blocks_the_start_and_explains_why(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    intruder = ContainerRecord(id="x1", name=f"{scope.project_name}-web-1", state="running")
    adapter = FakeAdapter(inventory=ResourceInventory(scope.project_name, containers=(intruder,)))

    result = start(profile(), root, adapter=adapter, environ={})

    assert result.code == "UP_OWNERSHIP_CONFLICT"
    assert adapter.starts == []
    assert any("Refusing to act" in line for line in result.lines)


def test_incomplete_inventory_blocks_the_start(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    adapter = FakeAdapter(
        inventory=ResourceInventory(scope.project_name, errors=("Network query failed.",))
    )

    result = start(profile(), root, adapter=adapter, environ={})

    assert result.code == "UP_OWNERSHIP_CONFLICT"
    assert adapter.starts == []


# --- runtime failures ------------------------------------------------------


def test_failed_start_summarizes_stderr_and_redacts_it(tmp_path: Path) -> None:
    adapter = FakeAdapter(
        outcome=OperationOutcome("failed", 1, "", "bind failed for hunter2-very-secret")
    )

    result = start(
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

    result = start(profile(), make_project(tmp_path), adapter=adapter, environ={}, timeout=5)

    assert result.code == "UP_TIMED_OUT"
    assert "5 seconds" in result.summary
    assert any("may have been created" in line for line in result.lines)


def test_unavailable_runtime_during_start_is_reported(tmp_path: Path) -> None:
    adapter = FakeAdapter(outcome=OperationOutcome("unavailable"))

    result = start(profile(), make_project(tmp_path), adapter=adapter, environ={})

    assert result.code == "UP_RUNTIME_UNAVAILABLE"


# --- output control --------------------------------------------------------


def test_default_output_is_a_bounded_tail(tmp_path: Path) -> None:
    noisy = "\n".join(f"line {index}" for index in range(60))
    adapter = FakeAdapter(outcome=OperationOutcome("failed", 1, "", noisy))

    result = start(profile(), make_project(tmp_path), adapter=adapter, environ={})

    assert any("earlier line(s) hidden" in line for line in result.lines)
    assert not any("Runtime stdout:" in line for line in result.lines)


def test_show_output_includes_both_streams_and_still_redacts(tmp_path: Path) -> None:
    adapter = FakeAdapter(
        outcome=OperationOutcome("succeeded", 0, "pulled image-layer-token", "warning text")
    )

    result = start(
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

    shown = start(
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
        self,
        *,
        project_name: str,
        project_root: Path,
        compose_file: Path,
        timeout: float,
        cancel: object = None,
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
    assert "Will remove" not in text.split("Will leave")[0].split("Will stop")[1]
    assert "network(s) in place" in text  # legacy is running and holds the network
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
    assert "network(s) still exist" not in text  # the running leftover is expected to hold it


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
        result = start(profile(ports=[port]), root, adapter=adapter, environ={})

    assert result.ok and result.code == "UP_STARTED"
    assert len(adapter.starts) == 1
    assert any("PORT_UNAVAILABLE" in line for line in result.lines)


def test_other_preflight_failures_still_block_a_start(tmp_path: Path) -> None:
    adapter = FakeAdapter()

    result = start(
        profile(required_env=["ABSENT_VARIABLE"]),
        make_project(tmp_path),
        adapter=adapter,
        environ={},
    )

    assert result.code == "UP_PREFLIGHT_FAILED" and adapter.starts == []


# --- socket permission problems --------------------------------------------

DENIED = ComposeCapabilities(
    executable="docker",
    compose_cli="available",
    compose_cli_returncode=0,
    daemon="permission_denied",
)


def test_up_refuses_with_a_permission_code_and_runs_nothing(tmp_path: Path) -> None:
    adapter = FakeAdapter(capabilities=DENIED)

    result = start(profile(), make_project(tmp_path), adapter=adapter, environ={})

    assert result.code == "UP_PERMISSION_DENIED" and adapter.starts == []
    assert "docker" in (result.remedy or "").lower()


def test_status_and_down_use_the_same_permission_code_family(tmp_path: Path) -> None:
    root = make_project(tmp_path)

    status = project_status(profile(), root, adapter=StatusAdapter(capabilities=DENIED))
    plan = plan_stop(profile(), root, adapter=StopAdapter(capabilities=DENIED))

    assert status.code == "STATUS_PERMISSION_DENIED"
    assert isinstance(plan, LifecycleResult) and plan.code == "DOWN_PERMISSION_DENIED"


def test_successful_start_hides_runtime_output_unless_requested(tmp_path: Path) -> None:
    """DevDock already described what changed; the raw text only adds leak risk."""
    root = make_project(tmp_path)
    noisy = OperationOutcome("succeeded", 0, "", "Container web Created\nContainer web Started")

    quiet = start(profile(), root, adapter=FakeAdapter(outcome=noisy), environ={})
    verbose = start(
        profile(), root, adapter=FakeAdapter(outcome=noisy), environ={}, show_output=True
    )

    assert quiet.ok and "Container web Started" not in "\n".join(quiet.lines)
    assert "Container web Started" in "\n".join(verbose.lines)


def test_successful_stop_hides_runtime_output_unless_requested(tmp_path: Path) -> None:
    root, _scope, inventory = running_project(tmp_path)
    noisy = OperationOutcome("succeeded", 0, "", "Container web Removed")
    adapter = StopAdapter(inventory=inventory, stop_outcome=noisy)
    plan = plan_stop(profile(), root, adapter=adapter)
    assert isinstance(plan, StopPlan)

    quiet = execute_stop(plan, adapter=adapter, environ={})

    assert "Container web Removed" not in "\n".join(quiet.lines)
    assert any("Preserved" in line for line in quiet.lines)


def test_advisory_findings_are_not_labelled_as_failures(tmp_path: Path) -> None:
    """An [OK] result must not contain a line that reads [FAIL]."""
    root = make_project(tmp_path)

    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        result = start(
            profile(ports=[held.getsockname()[1]]),
            root,
            adapter=FakeAdapter(),
            environ={},
        )

    text = "\n".join(result.lines)
    assert result.ok
    assert "[FAIL]" not in text
    assert "[ADVISORY] PORT_UNAVAILABLE" in text
    assert "did not block this start" in text


def test_networks_kept_for_an_undeclared_container_are_planned_and_not_a_failure(
    tmp_path: Path,
) -> None:
    """Leaving an undeclared container means its network must survive too.

    Reporting that survival as DOWN_PARTIAL told the user to retry a stop that
    could never complete. The plan now says the network stays, and the
    verification treats that as the expected outcome.
    """
    root, scope, inventory = running_project(tmp_path)
    legacy = inventory.containers[2]
    after = ResourceInventory(
        scope.project_name,
        containers=(legacy,),
        networks=inventory.networks,  # still in use by the legacy container
        volumes=inventory.volumes,
    )
    adapter = StopAdapter(inventory=inventory, after=after)
    plan = plan_stop(profile(), root, adapter=adapter)
    assert isinstance(plan, StopPlan)
    preview = "\n".join(plan.explain())

    assert plan.networks_removable == ()
    assert (
        "Will remove" not in preview.split("Will leave")[0]
        or "network" not in preview.split("Will leave")[0]
    )
    assert "network(s) in place" in preview
    assert "docker rm" in preview

    result = execute_stop(plan, adapter=adapter, environ={})

    assert result.ok and result.code == "DOWN_STOPPED", "\n".join(result.lines)
    assert "Warning" not in "\n".join(result.lines)
    assert "Left in place as planned: 1 container(s)" in "\n".join(result.lines)


def test_only_undeclared_containers_left_means_nothing_to_stop(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    legacy = managed(scope, f"{scope.project_name}-legacy-1", "legacy")
    inventory = ResourceInventory(
        scope.project_name,
        containers=(legacy,),
        networks=(NetworkRecord("n1", f"{scope.project_name}_default"),),
    )
    adapter = StopAdapter(inventory=inventory)

    plan = plan_stop(profile(), root, adapter=adapter)

    assert isinstance(plan, StopPlan) and plan.nothing_to_do
    result = execute_stop(plan, adapter=adapter, environ={})
    assert result.code == "DOWN_NOTHING_TO_STOP" and adapter.stops == []


def test_surviving_network_without_a_reason_is_still_partial(tmp_path: Path) -> None:
    root, scope, inventory = running_project(tmp_path)
    no_remaining = ResourceInventory(
        scope.project_name, containers=inventory.containers[:2], networks=inventory.networks
    )
    after = ResourceInventory(scope.project_name, networks=inventory.networks)
    adapter = StopAdapter(inventory=no_remaining, after=after)
    plan = plan_stop(profile(), root, adapter=adapter)
    assert isinstance(plan, StopPlan) and not plan.remaining

    result = execute_stop(plan, adapter=adapter, environ={})

    assert result.code == "DOWN_PARTIAL"


def test_a_stopped_leftover_does_not_hold_the_network(tmp_path: Path) -> None:
    """Docker releases endpoints on stop, so Compose will remove the network."""
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    web = managed(scope, f"{scope.project_name}-web-1", "web")
    legacy = managed(scope, f"{scope.project_name}-legacy-1", "legacy", state="exited")
    network = NetworkRecord("n1", f"{scope.project_name}_default")
    inventory = ResourceInventory(scope.project_name, containers=(web, legacy), networks=(network,))
    after = ResourceInventory(scope.project_name, containers=(legacy,))  # network gone
    adapter = StopAdapter(inventory=inventory, after=after)
    plan = plan_stop(profile(), root, adapter=adapter)
    assert isinstance(plan, StopPlan)

    assert plan.remaining == (legacy,) and plan.remaining_in_use == ()
    assert plan.networks_removable == (network,)
    assert "Will remove 1 project network(s)" in "\n".join(plan.explain())
    assert execute_stop(plan, adapter=adapter, environ={}).code == "DOWN_STOPPED"


def test_timeout_evidence_includes_the_inspect_command_it_refers_to(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    adapter = FakeAdapter(outcome=OperationOutcome("timed_out"))

    result = start(profile(), root, adapter=adapter, environ={}, timeout=3)

    text = "\n".join(result.lines)
    assert "docker ps command above" in (result.remedy or "")
    assert "docker ps -a --filter label=com.docker.compose.project=" in text


def test_all_three_commands_share_identical_gate_wording(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    denied = ComposeCapabilities(
        executable="docker",
        compose_cli="available",
        compose_cli_returncode=0,
        daemon="permission_denied",
    )
    up = start(profile(), root, adapter=FakeAdapter(capabilities=denied), environ={})
    status = project_status(profile(), root, adapter=StatusAdapter(capabilities=denied))
    down = plan_stop(profile(), root, adapter=StopAdapter(capabilities=denied))
    assert isinstance(down, LifecycleResult)

    assert {up.remedy, status.remedy, down.remedy} == {up.remedy}
    assert all(
        r.summary.endswith("this user may not connect to the Docker daemon.")
        for r in (up, status, down)
    )


def test_status_probes_the_services_the_profile_declares(tmp_path: Path) -> None:
    """The profile's probes must reach the prober, and their results the report."""

    asked: list[int] = []

    def prober(probe: object) -> ProbeResult:
        asked.append(probe.port)  # type: ignore[attr-defined]
        return ProbeResult(True, "answered 200")

    root = make_project(tmp_path)
    declaring = ProjectProfile.model_validate(
        {
            "schema_version": 1,
            "project": "sample",
            "compose_file": "compose.yaml",
            "services": {"web": {"readiness": [{"type": "http", "port": 18080}]}},
        }
    )
    scope = ownership_scope(declaring, root)
    web = managed(scope, f"{scope.project_name}-web-1", "web")
    adapter = StatusAdapter(
        declared=DeclaredServices(("web",)),
        inventory=ResourceInventory(scope.project_name, containers=(web,)),
        observed=ServiceObservations(
            items=(ServiceObservation(web.id, web.name, "web", "running", "", None),)
        ),
    )

    result = project_status(declaring, root, adapter=adapter, probe=prober)

    assert asked == [18080]
    assert result.ok
    assert any("[PASS] http GET 127.0.0.1:18080/ answered 200" in line for line in result.lines)


def test_status_does_not_probe_a_service_the_profile_does_not_declare(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    web = managed(scope, f"{scope.project_name}-web-1", "web")
    adapter = StatusAdapter(
        declared=DeclaredServices(("web",)),
        inventory=ResourceInventory(scope.project_name, containers=(web,)),
        observed=ServiceObservations(
            items=(ServiceObservation(web.id, web.name, "web", "running", "", None),)
        ),
    )

    def refuse(probe: object) -> ProbeResult:
        raise AssertionError("no probe should run for an undeclared service")

    assert project_status(profile(), root, adapter=adapter, probe=refuse).ok


# --- the graph gate and waiting -------------------------------------------


class WaitAdapter(StatusAdapter):
    """A status adapter that also answers the dependency and observation queries."""

    def __init__(
        self,
        *,
        dependency_edges: tuple[DeclaredDependency, ...] = (),
        snapshots: tuple[ServiceObservations, ...] = (),
        health_disabled: frozenset[str] = frozenset(),
        dependency_error: str | None = None,
        unsupported: tuple[str, ...] = (),
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._edges = dependency_edges
        self._snapshots = list(snapshots)
        self._health_disabled = health_disabled
        self._dependency_error = dependency_error
        self._unsupported = unsupported

    def list_dependencies(self, *, project_root: Path, compose_file: Path) -> DeclaredDependencies:
        if self._unsupported:
            return DeclaredDependencies(unsupported=self._unsupported)
        if self._dependency_error:
            return DeclaredDependencies(error=self._dependency_error)
        return DeclaredDependencies(
            services=self._declared.names,
            dependencies=self._edges,
            healthcheck_disabled=self._health_disabled,
        )

    def observe_services(
        self, *, project_name: str, project_root: Path, compose_file: Path
    ) -> ServiceObservations:
        self.observe_calls.append(project_name)
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0] if self._snapshots else ServiceObservations()


class TickingClock:
    """Advances only when the wait sleeps, so no test spends real time."""

    def __init__(self) -> None:
        self.time = 0.0

    def now(self) -> float:
        return self.time

    def sleep(self, seconds: float) -> None:
        self.time += seconds


def healthy(scope: object, service: str, health: str = "healthy") -> ServiceObservation:
    name = f"{scope.project_name}-{service}-1"  # type: ignore[attr-defined]
    return ServiceObservation(f"id-{service}", name, service, "running", health, None, ())


def test_a_dependency_cycle_refuses_before_anything_is_started(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    adapter = WaitAdapter(
        declared=DeclaredServices(("web", "db")),
        dependency_edges=(
            DeclaredDependency("web", "db", "service_started"),
            DeclaredDependency("db", "web", "service_started"),
        ),
    )

    result = start_project(profile(), root, adapter=adapter, environ={})

    assert result.code == "UP_DEPENDENCY_CYCLE"
    assert adapter.starts == [], "nothing may be started when the graph is refused"
    assert any("depends on" in line for line in result.lines)


def test_a_dependency_on_a_missing_service_refuses_before_starting(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    adapter = WaitAdapter(
        declared=DeclaredServices(("web",)),
        dependency_edges=(DeclaredDependency("web", "ghost", "service_started"),),
    )

    result = start_project(profile(), root, adapter=adapter, environ={})

    assert result.code == "UP_DEPENDENCY_UNDECLARED"
    assert adapter.starts == []


def test_a_profile_naming_an_unknown_service_refuses_before_starting(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    adapter = WaitAdapter(declared=DeclaredServices(("web",)))

    result = start_project(
        profile(services={"typo": {"readiness": []}}), root, adapter=adapter, environ={}
    )

    assert result.code == "UP_SERVICE_UNDECLARED"
    assert adapter.starts == []


def test_a_condition_this_version_cannot_order_against_refuses(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    adapter = WaitAdapter(
        declared=DeclaredServices(("web",)), unsupported=("Compose reported something new.",)
    )

    result = start_project(profile(), root, adapter=adapter, environ={})

    assert result.code == "UP_DEPENDENCY_UNSUPPORTED"
    assert adapter.starts == []
    assert "--no-wait" in (result.remedy or "")


def test_unreadable_dependencies_warn_and_still_start(tmp_path: Path) -> None:
    """Ordering is lost; the project is not."""

    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    adapter = WaitAdapter(
        declared=DeclaredServices(("web",)),
        dependency_error="Compose dependency listing failed with exit code 1.",
        inventory=ResourceInventory(
            scope.project_name, containers=(managed(scope, f"{scope.project_name}-web-1", "web"),)
        ),
        snapshots=(ServiceObservations(items=(healthy(scope, "web"),)),),
    )

    result = start_project(profile(), root, adapter=adapter, environ={}, clock=TickingClock())

    assert result.ok and result.code == "UP_READY"
    assert any("GRAPH_UNAVAILABLE" in line for line in result.lines)
    assert adapter.starts, "the project should still be started"


def test_a_successful_wait_reports_ready_and_shows_the_order(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    adapter = WaitAdapter(
        declared=DeclaredServices(("web", "db")),
        dependency_edges=(DeclaredDependency("web", "db", "service_healthy"),),
        inventory=ResourceInventory(
            scope.project_name,
            containers=(
                managed(scope, f"{scope.project_name}-web-1", "web"),
                managed(scope, f"{scope.project_name}-db-1", "db"),
            ),
        ),
        snapshots=(ServiceObservations(items=(healthy(scope, "web"), healthy(scope, "db"))),),
    )

    result = start_project(profile(), root, adapter=adapter, environ={}, clock=TickingClock())
    text = "\n".join(result.lines)

    assert result.ok and result.code == "UP_READY"
    assert "Startup order (2 layers)" in text
    assert "web depends on db (service_healthy" in text


def test_a_service_that_never_becomes_ready_times_out_with_its_last_result(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    adapter = WaitAdapter(
        declared=DeclaredServices(("web",)),
        inventory=ResourceInventory(
            scope.project_name, containers=(managed(scope, f"{scope.project_name}-web-1", "web"),)
        ),
        snapshots=(ServiceObservations(items=(healthy(scope, "web", "starting"),)),),
    )

    result = start_project(
        profile(), root, adapter=adapter, environ={}, clock=TickingClock(), ready_timeout=5
    )

    assert not result.ok and result.code == "UP_READINESS_TIMEOUT"
    assert any("compose health reports starting" in line for line in result.lines)
    assert "--ready-timeout" in (result.remedy or "")


def test_no_wait_reports_the_start_and_says_readiness_was_not_verified(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path)
    adapter = WaitAdapter(declared=DeclaredServices(("web",)))

    result = start_project(profile(), root, adapter=adapter, environ={}, wait=False)

    assert result.ok and result.code == "UP_STARTED"
    assert any("readiness was not verified" in line for line in result.lines)
    assert adapter.observe_calls == [], "no-wait must not poll"


def test_a_service_with_nothing_to_check_is_reported_as_unverified(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    adapter = WaitAdapter(
        declared=DeclaredServices(("web",)),
        inventory=ResourceInventory(
            scope.project_name, containers=(managed(scope, f"{scope.project_name}-web-1", "web"),)
        ),
        snapshots=(ServiceObservations(items=(healthy(scope, "web", ""),)),),
    )

    result = start_project(profile(), root, adapter=adapter, environ={}, clock=TickingClock())

    assert result.ok and result.code == "UP_READY_UNVERIFIED"
    assert any("not that they are serving" in line for line in result.lines)


def test_require_ready_refuses_to_accept_an_unverifiable_service(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    adapter = WaitAdapter(
        declared=DeclaredServices(("web",)),
        inventory=ResourceInventory(
            scope.project_name, containers=(managed(scope, f"{scope.project_name}-web-1", "web"),)
        ),
        snapshots=(ServiceObservations(items=(healthy(scope, "web", ""),)),),
    )

    result = start_project(
        profile(),
        root,
        adapter=adapter,
        environ={},
        clock=TickingClock(),
        ready_timeout=5,
        require_ready=True,
    )

    assert not result.ok and result.code == "UP_READINESS_TIMEOUT"


def test_wait_for_project_starts_nothing(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    adapter = WaitAdapter(
        declared=DeclaredServices(("web",)),
        inventory=ResourceInventory(
            scope.project_name, containers=(managed(scope, f"{scope.project_name}-web-1", "web"),)
        ),
        snapshots=(ServiceObservations(items=(healthy(scope, "web"),)),),
    )

    result = wait_for_project(profile(), root, adapter=adapter, clock=TickingClock())

    assert result.ok and result.code == "WAIT_READY"
    assert adapter.starts == []


def test_wait_for_project_refuses_a_bad_graph_with_its_own_prefix(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    adapter = WaitAdapter(
        declared=DeclaredServices(("web", "db")),
        dependency_edges=(
            DeclaredDependency("web", "db", "service_started"),
            DeclaredDependency("db", "web", "service_started"),
        ),
    )

    assert wait_for_project(profile(), root, adapter=adapter).code == "WAIT_DEPENDENCY_CYCLE"


def test_a_cancelled_wait_leaves_the_project_alone(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    token = CancelToken()
    token.cancel()
    adapter = WaitAdapter(
        declared=DeclaredServices(("web",)),
        inventory=ResourceInventory(
            scope.project_name, containers=(managed(scope, f"{scope.project_name}-web-1", "web"),)
        ),
    )

    result = wait_for_project(profile(), root, adapter=adapter, cancel=token)

    assert result.code == "WAIT_INTERRUPTED"
    assert any("Nothing was stopped or removed" in line for line in result.lines)


def test_an_ownership_conflict_is_reported_before_the_graph_is_even_read(
    tmp_path: Path,
) -> None:
    """Ownership is the safety gate; a user hears about it first, and it costs less."""

    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    squatter = ContainerRecord(id="q", name=f"{scope.project_name}-web-1", state="running")
    adapter = WaitAdapter(
        declared=DeclaredServices(("web", "db")),
        # A graph bad enough to refuse on, which must not be what is reported.
        dependency_edges=(
            DeclaredDependency("web", "db", "service_started"),
            DeclaredDependency("db", "web", "service_started"),
        ),
        inventory=ResourceInventory(scope.project_name, containers=(squatter,)),
    )

    result = start_project(profile(), root, adapter=adapter, environ={})

    assert result.code == "UP_OWNERSHIP_CONFLICT"
    assert adapter.starts == []


def test_a_bad_graph_is_still_caught_when_ownership_is_clean(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    adapter = WaitAdapter(
        declared=DeclaredServices(("web", "db")),
        dependency_edges=(
            DeclaredDependency("web", "db", "service_started"),
            DeclaredDependency("db", "web", "service_started"),
        ),
    )

    result = start_project(profile(), root, adapter=adapter, environ={})

    assert result.code == "UP_DEPENDENCY_CYCLE"
    assert adapter.starts == []


# --- staged startup --------------------------------------------------------


def chain_adapter(scope: object, ready_from: int = 0) -> WaitAdapter:
    """db -> web -> worker, all healthy once they appear in the inventory.

    The fake only reports a container once its service has been started, so
    the engine sees exactly what a staged start would produce.
    """

    started: list[str] = []
    name = scope.project_name  # type: ignore[attr-defined]

    class Chained(WaitAdapter):
        def list_project_resources(self, project_name: str) -> ResourceInventory:
            return ResourceInventory(
                project_name,
                containers=tuple(
                    managed(scope, f"{name}-{service}-1", service) for service in started
                ),
            )

        def observe_services(
            self, *, project_name: str, project_root: Path, compose_file: Path
        ) -> ServiceObservations:
            self.observe_calls.append(project_name)
            return ServiceObservations(items=tuple(healthy(scope, service) for service in started))

        def start(self, **kwargs: object) -> OperationOutcome:
            started.extend(kwargs.get("services") or ())  # type: ignore[arg-type]
            return super().start(**kwargs)  # type: ignore[arg-type]

    return Chained(
        declared=DeclaredServices(("db", "web", "worker")),
        dependency_edges=(
            DeclaredDependency("web", "db", "service_healthy"),
            DeclaredDependency("worker", "web", "service_healthy"),
        ),
    )


def test_a_chain_is_started_one_layer_at_a_time(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    adapter = chain_adapter(scope)

    result = start_project(profile(), root, adapter=adapter, environ={}, clock=TickingClock())

    assert result.ok, "\n".join(result.lines)
    assert [call["services"] for call in adapter.starts] == [("db",), ("web",), ("worker",)]


def test_a_staged_start_tells_compose_not_to_reorder_what_it_is_given(
    tmp_path: Path,
) -> None:
    """DevDock has already satisfied the dependencies of each batch it sends."""

    root = make_project(tmp_path)
    adapter = chain_adapter(ownership_scope(profile(), root))

    start_project(profile(), root, adapter=adapter, environ={}, clock=TickingClock())

    assert all(call["no_deps"] is True for call in adapter.starts)


def test_independent_services_go_out_in_one_batch(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    adapter = WaitAdapter(
        declared=DeclaredServices(("a", "b", "c")),
        inventory=ResourceInventory(
            scope.project_name,
            containers=tuple(
                managed(scope, f"{scope.project_name}-{s}-1", s) for s in ("a", "b", "c")
            ),
        ),
        snapshots=(ServiceObservations(items=tuple(healthy(scope, s) for s in ("a", "b", "c"))),),
    )

    start_project(profile(), root, adapter=adapter, environ={}, clock=TickingClock())

    assert [call["services"] for call in adapter.starts] == [("a", "b", "c")]


def test_an_already_running_project_is_still_reconciled(tmp_path: Path) -> None:
    """A repeat `up` must not become a no-op just because everything is ready."""

    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    adapter = WaitAdapter(
        declared=DeclaredServices(("web",)),
        inventory=ResourceInventory(
            scope.project_name, containers=(managed(scope, f"{scope.project_name}-web-1", "web"),)
        ),
        snapshots=(ServiceObservations(items=(healthy(scope, "web"),)),),
    )

    result = start_project(profile(), root, adapter=adapter, environ={}, clock=TickingClock())

    assert result.ok
    assert [call["services"] for call in adapter.starts] == [("web",)]


def test_no_stage_starts_the_whole_project_in_one_unscoped_call(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    adapter = chain_adapter(ownership_scope(profile(), root))

    start_project(
        profile(),
        root,
        adapter=adapter,
        environ={},
        clock=TickingClock(),
        stage=False,
        ready_timeout=1,
        deadline=5,
    )

    assert [call["services"] for call in adapter.starts] == [()]
    assert adapter.starts[0]["no_deps"] is False


def test_a_degraded_graph_leaves_the_ordering_to_compose(tmp_path: Path) -> None:
    """Without the Compose dependencies, DevDock must not claim to know the order."""

    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    adapter = WaitAdapter(
        declared=DeclaredServices(("web",)),
        dependency_error="Compose dependency listing failed with exit code 1.",
        inventory=ResourceInventory(
            scope.project_name, containers=(managed(scope, f"{scope.project_name}-web-1", "web"),)
        ),
        snapshots=(ServiceObservations(items=(healthy(scope, "web"),)),),
    )

    start_project(profile(), root, adapter=adapter, environ={}, clock=TickingClock())

    assert adapter.starts[0]["no_deps"] is False, "a guessed graph must not suppress deps"


def test_max_parallel_reaches_the_runtime(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    adapter = chain_adapter(ownership_scope(profile(), root))

    start_project(
        profile(), root, adapter=adapter, environ={}, clock=TickingClock(), max_parallel=2
    )

    assert all(call["max_parallel"] == 2 for call in adapter.starts)


def test_a_batch_that_fails_to_start_is_reported_with_its_own_code(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    adapter = WaitAdapter(
        declared=DeclaredServices(("web",)),
        outcome=OperationOutcome("failed", 1, "", "manifest unknown"),
    )

    result = start_project(profile(), root, adapter=adapter, environ={}, clock=TickingClock())

    assert not result.ok and result.code == "UP_IMAGE_NOT_FOUND"
    assert "while starting web" in result.summary


def test_a_later_layer_is_not_started_after_an_earlier_one_fails(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    adapter = chain_adapter(scope)
    adapter._outcome = OperationOutcome("failed", 1, "", "no space left on device")

    result = start_project(profile(), root, adapter=adapter, environ={}, clock=TickingClock())

    assert result.code == "UP_RESOURCE_EXHAUSTED"
    assert [call["services"] for call in adapter.starts] == [("db",)]


def test_max_parallel_still_reaches_the_runtime_without_staging(tmp_path: Path) -> None:
    """A flag that is quietly ignored by one path is worse than no flag."""

    root = make_project(tmp_path)
    adapter = WaitAdapter(declared=DeclaredServices(("web",)))

    start_project(profile(), root, adapter=adapter, environ={}, wait=False, max_parallel=4)

    assert adapter.starts[0]["max_parallel"] == 4


def test_a_project_with_no_services_starts_nothing_and_says_it_is_ready(
    tmp_path: Path,
) -> None:
    """Deliberate: there is nothing to start, so no Compose call is made.

    A Compose file whose services are all behind inactive profiles looks like
    this, and `docker compose up` would do nothing for it either.
    """

    root = make_project(tmp_path, compose="services: {}\n")
    adapter = WaitAdapter(declared=DeclaredServices(()))

    result = start_project(profile(), root, adapter=adapter, environ={}, clock=TickingClock())

    assert result.ok and result.code == "UP_READY"
    assert adapter.starts == []


def test_an_interrupt_between_layers_stops_the_start(tmp_path: Path) -> None:
    """Cancelling must not let the next layer go out after the signal arrived."""

    root = make_project(tmp_path)
    scope = ownership_scope(profile(), root)
    token = CancelToken()
    adapter = chain_adapter(scope)

    class CancelAfterFirst(type(adapter)):  # type: ignore[misc]
        def start(self, **kwargs: object) -> OperationOutcome:
            outcome = super().start(**kwargs)  # type: ignore[arg-type]
            token.cancel()
            return outcome

    cancelling = CancelAfterFirst(
        declared=DeclaredServices(("db", "web", "worker")),
        dependency_edges=(
            DeclaredDependency("web", "db", "service_healthy"),
            DeclaredDependency("worker", "web", "service_healthy"),
        ),
    )

    result = start_project(
        profile(), root, adapter=cancelling, environ={}, clock=TickingClock(), cancel=token
    )

    assert result.code == "UP_INTERRUPTED"
    assert len(cancelling.starts) == 1, "no further layer may be started after a cancel"
    assert any("Nothing was removed" in line for line in result.lines)
