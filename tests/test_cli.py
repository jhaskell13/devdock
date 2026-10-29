from pathlib import Path

import pytest
from typer.testing import CliRunner

import devdock.adapters.compose as compose
import devdock.cli as cli
from devdock.cli import app
from devdock.lifecycle import LifecycleResult, StopPlan

runner = CliRunner()


def test_init_inspect_and_check_work_as_a_minimal_path(tmp_path: Path) -> None:
    initialized = runner.invoke(app, ["init", str(tmp_path)])
    assert initialized.exit_code == 0
    assert (tmp_path / "devdock.yaml").is_file()

    inspected = runner.invoke(app, ["inspect", str(tmp_path)])
    assert inspected.exit_code == 0
    assert "Configured project:" in inspected.stdout

    checked = runner.invoke(app, ["check", str(tmp_path)])
    assert checked.exit_code == 0
    assert "[PASS] PROFILE_VALID: Profile is valid" in checked.stdout


def test_init_never_overwrites_an_existing_profile(tmp_path: Path) -> None:
    profile_path = tmp_path / "devdock.yaml"
    profile_path.write_text("user content\n", encoding="utf-8")

    result = runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 1
    assert profile_path.read_text(encoding="utf-8") == "user content\n"


def test_check_requires_a_profile(tmp_path: Path) -> None:
    result = runner.invoke(app, ["check", str(tmp_path)])

    assert result.exit_code == 1
    assert "devdock.yaml was not found" in result.stderr


def test_check_shows_target_interpreter_warning_without_failing(tmp_path: Path) -> None:
    (tmp_path / "devdock.yaml").write_text("schema_version: 1\nproject: sample\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.12"\n', encoding="utf-8"
    )

    result = runner.invoke(app, ["check", str(tmp_path)])

    assert result.exit_code == 0
    assert "[WARN] PROJECT_PYTHON_METADATA" in result.stdout


def test_check_reports_skipped_dependency_checks_and_preserves_failure_exit(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "devdock.yaml").write_text(
        "schema_version: 1\nproject: sample\nruntimes:\n  docker: compose\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(compose.shutil, "which", lambda _: None)

    result = runner.invoke(app, ["check", str(tmp_path)])

    assert result.exit_code == 1
    assert "[FAIL] DOCKER_CLI_MISSING" in result.stdout
    assert "[SKIP] DOCKER_DAEMON_SKIPPED" in result.stdout
    assert "[SKIP] COMPOSE_CONFIG_SKIPPED" in result.stdout


def test_inspect_reports_invalid_profile(tmp_path: Path) -> None:
    (tmp_path / "devdock.yaml").write_text("schema_version: 99\n", encoding="utf-8")

    result = runner.invoke(app, ["inspect", str(tmp_path)])

    assert result.exit_code == 1
    assert "Profile error:" in result.stderr


def test_inspect_reports_discovery_evidence_and_known_files(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")

    result = runner.invoke(app, ["inspect", str(tmp_path)])

    assert result.exit_code == 0
    assert "Discovery evidence: compose.yaml" in result.stdout
    assert "Known project files: compose.yaml, pyproject.toml" in result.stdout


def test_inspect_explains_nested_roots_and_unicode_paths(tmp_path: Path) -> None:
    outer = tmp_path / "outer project"
    inner = outer / "café"
    inner.mkdir(parents=True)
    (outer / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (inner / "devdock.yaml").write_text("schema_version: 1\nproject: nested\n", encoding="utf-8")

    result = runner.invoke(app, ["inspect", str(inner)])

    assert result.exit_code == 0
    assert str(inner.resolve()) in result.stdout
    assert "Nested project roots detected" in result.stdout


def test_init_reports_when_directory_cannot_be_created(tmp_path: Path) -> None:
    directory_is_a_file = tmp_path / "not-a-directory"
    directory_is_a_file.write_text("file\n", encoding="utf-8")

    result = runner.invoke(app, ["init", str(directory_is_a_file / "child")])

    assert result.exit_code == 1
    assert "Could not create project directory" in result.stderr


def test_inspect_reports_the_compose_project_name_devdock_will_manage(tmp_path: Path) -> None:
    (tmp_path / "devdock.yaml").write_text(
        "schema_version: 1\nproject: Sample App\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["inspect", str(tmp_path)])

    assert result.exit_code == 0
    assert "Compose project name: devdock-sample-app-" in result.stdout


def test_up_refuses_without_a_compose_file_and_exits_non_zero(tmp_path: Path) -> None:
    (tmp_path / "devdock.yaml").write_text("schema_version: 1\nproject: sample\n", encoding="utf-8")

    result = runner.invoke(app, ["up", str(tmp_path)])

    assert result.exit_code == 1
    assert "[REFUSED] UP_COMPOSE_FILE_MISSING" in result.stderr
    assert "Suggested action:" in result.stderr


def test_up_requires_a_profile(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

    result = runner.invoke(app, ["up", str(tmp_path)])

    assert result.exit_code == 1
    assert "devdock.yaml was not found" in result.stderr


def test_up_reports_a_successful_start_on_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "devdock.yaml").write_text("schema_version: 1\nproject: sample\n", encoding="utf-8")
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

    def fake_start(*_args: object, **_kwargs: object) -> LifecycleResult:
        return LifecycleResult("UP_STARTED", True, "Started devdock-sample-x.", ("detail",))

    monkeypatch.setattr(cli, "start_project", fake_start)

    result = runner.invoke(app, ["up", str(tmp_path)])

    assert result.exit_code == 0
    assert "[OK] UP_STARTED: Started devdock-sample-x." in result.stdout
    assert "detail" in result.stdout


def test_up_rejects_a_non_positive_timeout(tmp_path: Path) -> None:
    result = runner.invoke(app, ["up", str(tmp_path), "--timeout", "0"])

    assert result.exit_code != 0


def test_status_renders_a_report_on_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "devdock.yaml").write_text("schema_version: 1\nproject: sample\n", encoding="utf-8")
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

    def fake_status(*_args: object, **_kwargs: object) -> LifecycleResult:
        return LifecycleResult("STATUS_REPORTED", True, "Status for x.", ("SERVICE  STATE",))

    monkeypatch.setattr(cli, "project_status", fake_status)

    result = runner.invoke(app, ["status", str(tmp_path)])

    assert result.exit_code == 0
    assert "[OK] STATUS_REPORTED" in result.stdout and "SERVICE  STATE" in result.stdout


def test_status_without_compose_file_exits_non_zero(tmp_path: Path) -> None:
    (tmp_path / "devdock.yaml").write_text("schema_version: 1\nproject: sample\n", encoding="utf-8")

    result = runner.invoke(app, ["status", str(tmp_path)])

    assert result.exit_code == 1
    assert "STATUS_COMPOSE_FILE_MISSING" in result.stderr


def _ready_project(tmp_path: Path) -> None:
    (tmp_path / "devdock.yaml").write_text("schema_version: 1\nproject: sample\n", encoding="utf-8")
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")


def _fake_plan(tmp_path: Path, *, empty: bool = False) -> StopPlan:
    from devdock.adapters import ContainerRecord, ResourceInventory
    from devdock.config import ProjectProfile
    from devdock.ownership import assess_ownership, ownership_scope

    scope = ownership_scope(ProjectProfile(schema_version=1, project="sample"), tmp_path)
    containers = (
        ()
        if empty
        else (
            ContainerRecord(
                "c1",
                f"{scope.project_name}-web-1",
                "running",
                scope.project_name,
                str(scope.root),
                str(scope.compose_file),
                "web",
                "False",
            ),
        )
    )
    inventory = ResourceInventory(scope.project_name, containers=containers)
    assessment = assess_ownership(scope, inventory)
    return StopPlan(scope, assessment, assessment.managed, (), (), ())


def test_down_dry_run_prints_the_plan_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_project(tmp_path)
    monkeypatch.setattr(cli, "plan_stop", lambda *_a, **_k: _fake_plan(tmp_path))
    monkeypatch.setattr(cli, "execute_stop", lambda *_a, **_k: pytest.fail("must not execute"))

    result = runner.invoke(app, ["down", str(tmp_path), "--dry-run"])

    assert result.exit_code == 0
    assert "Will stop and remove 1 container(s)" in result.stdout
    assert "DOWN_DRY_RUN" in result.stdout


def test_down_without_a_terminal_requires_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_project(tmp_path)
    monkeypatch.setattr(cli, "plan_stop", lambda *_a, **_k: _fake_plan(tmp_path))
    monkeypatch.setattr(cli, "execute_stop", lambda *_a, **_k: pytest.fail("must not execute"))
    monkeypatch.setattr(cli, "_interactive", lambda: False)

    result = runner.invoke(app, ["down", str(tmp_path)])

    assert result.exit_code == 1
    assert "DOWN_CONFIRMATION_REQUIRED" in result.stderr
    assert "Will stop and remove 1 container(s)" in result.stdout  # preview still shown


def test_down_with_yes_executes_the_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready_project(tmp_path)
    executed: list[StopPlan] = []

    def fake_execute(plan: StopPlan, **_kwargs: object) -> LifecycleResult:
        executed.append(plan)
        return LifecycleResult("DOWN_STOPPED", True, "Stopped.", ())

    monkeypatch.setattr(cli, "plan_stop", lambda *_a, **_k: _fake_plan(tmp_path))
    monkeypatch.setattr(cli, "execute_stop", fake_execute)

    result = runner.invoke(app, ["down", str(tmp_path), "--yes"])

    assert result.exit_code == 0 and len(executed) == 1
    assert "[OK] DOWN_STOPPED" in result.stdout


def test_down_interactive_decline_cancels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready_project(tmp_path)
    monkeypatch.setattr(cli, "plan_stop", lambda *_a, **_k: _fake_plan(tmp_path))
    monkeypatch.setattr(cli, "execute_stop", lambda *_a, **_k: pytest.fail("must not execute"))
    monkeypatch.setattr(cli, "_interactive", lambda: True)

    result = runner.invoke(app, ["down", str(tmp_path)], input="n\n")

    assert result.exit_code == 1
    assert "DOWN_CANCELLED" in result.stderr


def test_down_interactive_accept_executes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready_project(tmp_path)
    monkeypatch.setattr(cli, "plan_stop", lambda *_a, **_k: _fake_plan(tmp_path))
    monkeypatch.setattr(
        cli, "execute_stop", lambda *_a, **_k: LifecycleResult("DOWN_STOPPED", True, "Stopped.", ())
    )
    monkeypatch.setattr(cli, "_interactive", lambda: True)

    result = runner.invoke(app, ["down", str(tmp_path)], input="y\n")

    assert result.exit_code == 0 and "[OK] DOWN_STOPPED" in result.stdout


def test_down_with_nothing_to_do_needs_no_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_project(tmp_path)
    monkeypatch.setattr(cli, "plan_stop", lambda *_a, **_k: _fake_plan(tmp_path, empty=True))
    monkeypatch.setattr(
        cli,
        "execute_stop",
        lambda *_a, **_k: LifecycleResult("DOWN_NOTHING_TO_STOP", True, "Nothing.", ()),
    )
    monkeypatch.setattr(cli, "_interactive", lambda: False)

    result = runner.invoke(app, ["down", str(tmp_path)])

    assert result.exit_code == 0 and "DOWN_NOTHING_TO_STOP" in result.stdout


def test_down_refusal_from_planning_is_rendered(tmp_path: Path) -> None:
    (tmp_path / "devdock.yaml").write_text("schema_version: 1\nproject: sample\n", encoding="utf-8")

    result = runner.invoke(app, ["down", str(tmp_path), "--yes"])

    assert result.exit_code == 1
    assert "DOWN_COMPOSE_FILE_MISSING" in result.stderr


def test_version_flag_reports_the_package_version() -> None:
    from devdock import __version__

    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"devdock {__version__}"


def test_output_neutralizes_control_characters_from_runtime_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_project(tmp_path)
    hostile = "web\x1b[2J\x1b[H\x07 hidden\r\nfake [OK] line"

    def fake_status(*_args: object, **_kwargs: object) -> LifecycleResult:
        return LifecycleResult("STATUS_REPORTED", True, "Status.", (hostile,))

    monkeypatch.setattr(cli, "project_status", fake_status)

    result = runner.invoke(app, ["status", str(tmp_path)])

    assert "\x1b" not in result.stdout and "\x07" not in result.stdout
    assert "\r" not in result.stdout
    assert "web?[2J?[H? hidden??fake [OK] line" in result.stdout
