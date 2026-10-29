import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from devdock.adapters import CommandResult, ComposeAdapter, run_command
from devdock.config import ProjectProfile
from devdock.diagnostics import check_profile


class RecordingRunner:
    """Fake CommandRunner returning scripted results and recording argument arrays."""

    def __init__(self, results: list[CommandResult | BaseException]) -> None:
        self._results = iter(results)
        self.calls: list[tuple[list[str], Path | None, float]] = []

    def __call__(
        self, args: Sequence[str], *, cwd: Path | None = None, timeout: float
    ) -> CommandResult:
        self.calls.append((list(args), cwd, timeout))
        result = next(self._results)
        if isinstance(result, BaseException):
            raise result
        return result


OK = CommandResult(0, "", "")


def test_missing_docker_cli_short_circuits_without_running_commands() -> None:
    runner = RecordingRunner([])
    adapter = ComposeAdapter(runner, locate=lambda _: None)

    capabilities = adapter.detect_capabilities()

    assert capabilities.executable is None
    assert capabilities.compose_cli == "missing"
    assert capabilities.daemon == "not_checked"
    assert runner.calls == []


def test_compose_plugin_failure_prevents_daemon_check() -> None:
    runner = RecordingRunner([CommandResult(1, "", "plugin missing")])
    adapter = ComposeAdapter(runner, locate=lambda _: "/usr/bin/docker")

    capabilities = adapter.detect_capabilities()

    assert capabilities.compose_cli == "failed"
    assert capabilities.compose_cli_returncode == 1
    assert capabilities.daemon == "not_checked"
    assert [call[0] for call in runner.calls] == [["/usr/bin/docker", "compose", "version"]]


@pytest.mark.parametrize(
    "failure", [OSError("cannot start"), subprocess.TimeoutExpired(["docker"], 5)]
)
def test_unresponsive_compose_plugin_is_distinguished_from_failed(
    failure: BaseException,
) -> None:
    adapter = ComposeAdapter(RecordingRunner([failure]), locate=lambda _: "docker")

    assert adapter.detect_capabilities().compose_cli == "unresponsive"


def test_capabilities_use_argument_arrays_and_bounded_timeouts() -> None:
    runner = RecordingRunner([OK, OK])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    capabilities = adapter.detect_capabilities()

    assert capabilities.compose_available and capabilities.daemon_available
    assert [call[0] for call in runner.calls] == [
        ["docker", "compose", "version"],
        ["docker", "info", "--format", "{{.ServerVersion}}"],
    ]
    assert all(call[2] > 0 for call in runner.calls)


def test_daemon_timeout_reports_unavailable_but_keeps_compose_available() -> None:
    runner = RecordingRunner([OK, subprocess.TimeoutExpired(["docker"], 5)])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    capabilities = adapter.detect_capabilities()

    assert capabilities.compose_available
    assert capabilities.daemon == "unavailable"


def test_validate_config_runs_from_project_root_and_hides_output(tmp_path: Path) -> None:
    runner = RecordingRunner([CommandResult(1, "", "TOKEN=secret-value")])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")
    compose_path = tmp_path / "compose.yaml"

    validation = adapter.validate_config(compose_path, project_root=tmp_path)

    assert validation.outcome == "invalid"
    assert validation.returncode == 1
    assert "secret-value" not in repr(validation)
    args, cwd, _timeout = runner.calls[0]
    assert args == ["docker", "compose", "-f", str(compose_path), "config", "--quiet"]
    assert cwd == tmp_path


def test_validate_config_without_docker_or_on_timeout_is_unavailable(tmp_path: Path) -> None:
    missing = ComposeAdapter(RecordingRunner([]), locate=lambda _: None)
    timing_out = ComposeAdapter(
        RecordingRunner([subprocess.TimeoutExpired(["docker"], 10)]), locate=lambda _: "docker"
    )
    path = tmp_path / "compose.yaml"

    assert missing.validate_config(path, project_root=tmp_path).outcome == "unavailable"
    assert timing_out.validate_config(path, project_root=tmp_path).outcome == "unavailable"


def test_check_profile_accepts_an_injected_adapter(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    profile = ProjectProfile(
        schema_version=1,
        project="sample",
        runtimes={"docker": "compose"},
        compose_file=Path("compose.yaml"),
    )
    runner = RecordingRunner([OK, OK, OK])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    findings = check_profile(profile, tmp_path, adapter=adapter)
    codes = {item.code for item in findings}

    assert {"COMPOSE_CLI_AVAILABLE", "DOCKER_DAEMON_AVAILABLE", "COMPOSE_CONFIG_VALID"} <= codes
    assert len(runner.calls) == 3


def test_default_runner_never_uses_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "out", "err")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_command(("docker", "compose", "version"), timeout=1.5)

    assert result == CommandResult(0, "out", "err")
    assert captured["args"] == ["docker", "compose", "version"]
    assert captured.get("shell") in (None, False)
    assert captured["timeout"] == 1.5
    assert captured["capture_output"] is True and captured["check"] is False
