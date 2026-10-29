from pathlib import Path

import pytest

from devdock.adapters import (
    ComposeCapabilities,
    ContainerRecord,
    ResourceInventory,
    StartOutcome,
)
from devdock.config import ProjectProfile
from devdock.lifecycle import start_project
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
        outcome: StartOutcome | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._inventory = inventory
        self._outcome = outcome or StartOutcome("started", 0, "Container web Started", "")
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
    ) -> StartOutcome:
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
    result = start_project(
        profile(), make_project(tmp_path), adapter=FakeAdapter(), environ={}
    )

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
    adapter = FakeAdapter(
        inventory=ResourceInventory(scope.project_name, containers=(running,))
    )

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

    assert result.code == "UP_COMPOSE_FILE_OUTSIDE_PROJECT"
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
    intruder = ContainerRecord(
        id="x1", name=f"{scope.project_name}-web-1", state="running"
    )
    adapter = FakeAdapter(
        inventory=ResourceInventory(scope.project_name, containers=(intruder,))
    )

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
        outcome=StartOutcome("failed", 1, "", "bind failed for hunter2-very-secret")
    )

    result = start_project(
        profile(),
        make_project(tmp_path),
        adapter=adapter,
        environ={"DB_PASSWORD": "hunter2-very-secret"},
    )

    text = "\n".join(result.lines)
    assert result.code == "UP_FAILED" and "exit code 1" in result.summary
    assert "hunter2-very-secret" not in text
    assert "${DB_PASSWORD}" in text


def test_timeout_warns_that_containers_may_exist(tmp_path: Path) -> None:
    adapter = FakeAdapter(outcome=StartOutcome("timed_out", None, "pulling", ""))

    result = start_project(
        profile(), make_project(tmp_path), adapter=adapter, environ={}, timeout=5
    )

    assert result.code == "UP_TIMED_OUT"
    assert "5 seconds" in result.summary
    assert any("may have been created" in line for line in result.lines)


def test_unavailable_runtime_during_start_is_reported(tmp_path: Path) -> None:
    adapter = FakeAdapter(outcome=StartOutcome("unavailable"))

    result = start_project(profile(), make_project(tmp_path), adapter=adapter, environ={})

    assert result.code == "UP_RUNTIME_UNAVAILABLE"


# --- output control --------------------------------------------------------


def test_default_output_is_a_bounded_tail(tmp_path: Path) -> None:
    noisy = "\n".join(f"line {index}" for index in range(60))
    adapter = FakeAdapter(outcome=StartOutcome("failed", 1, "", noisy))

    result = start_project(profile(), make_project(tmp_path), adapter=adapter, environ={})

    assert any("earlier line(s) hidden" in line for line in result.lines)
    assert not any("Runtime stdout:" in line for line in result.lines)


def test_show_output_includes_both_streams_and_still_redacts(tmp_path: Path) -> None:
    adapter = FakeAdapter(
        outcome=StartOutcome("started", 0, "pulled image-layer-token", "warning text")
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
    adapter = FakeAdapter(outcome=StartOutcome("failed", 1, "", "secret-value-here"))

    shown = start_project(
        profile(),
        make_project(tmp_path),
        adapter=adapter,
        environ={"PW": "secret-value-here"},
        show_output=True,
    )

    assert "secret-value-here" not in "\n".join(shown.lines)
