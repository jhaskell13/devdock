import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import devdock.adapters.compose as compose
import devdock.diagnostics as diagnostics
from devdock.config import ProjectProfile
from devdock.diagnostics import check_profile


def test_unsupported_runtime_is_actionable(tmp_path: Path) -> None:
    profile = ProjectProfile(
        schema_version=1,
        project="sample",
        runtimes={"ruby": ">=3.2"},
    )

    findings = check_profile(profile, tmp_path)

    assert any(finding.severity == "FAIL" and "ruby" in finding.summary for finding in findings)


def test_unsupported_python_requirement_does_not_echo_configured_value(
    tmp_path: Path,
) -> None:
    profile = ProjectProfile(
        schema_version=1,
        project="sample",
        runtimes={"python": "TOKEN=private-value"},
    )

    findings = check_profile(profile, tmp_path)

    assert any(item.code == "PYTHON_REQUIREMENT_UNSUPPORTED" for item in findings)
    assert "private-value" not in repr(findings)


def test_unsupported_docker_requirement_is_categorized_and_marks_checks_skipped(
    tmp_path: Path,
) -> None:
    profile = ProjectProfile(
        schema_version=1,
        project="sample",
        runtimes={"docker": "TOKEN=private-value"},
    )

    findings = check_profile(profile, tmp_path)
    by_code = {item.code: item for item in findings}

    assert by_code["DOCKER_REQUIREMENT_UNSUPPORTED"].severity == "FAIL"
    assert by_code["DOCKER_CHECKS_SKIPPED"].severity == "SKIP"
    assert "private-value" not in repr(findings)


def test_missing_compose_file_is_reported(tmp_path: Path) -> None:
    profile = ProjectProfile(
        schema_version=1,
        project="sample",
        compose_file=Path("compose.yaml"),
    )

    findings = check_profile(profile, tmp_path)

    assert any(
        finding.severity == "FAIL" and "Compose file not found" in finding.summary
        for finding in findings
    )


def test_findings_have_stable_codes_and_interpreter_evidence(tmp_path: Path) -> None:
    profile = ProjectProfile(schema_version=1, project="sample", runtimes={"python": ">=3.12"})

    findings = check_profile(profile, tmp_path)

    python_finding = next(item for item in findings if item.code == "PYTHON_VERSION_OK")
    assert python_finding.evidence[0].startswith("Executable:")
    assert python_finding.evidence[1].startswith("Inspected version:")
    assert python_finding.doc_ref == "docs/cli.md#diagnostic-codes"


def test_python_minimum_patch_version_is_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        diagnostics,
        "sys",
        SimpleNamespace(
            version_info=SimpleNamespace(major=3, minor=12, micro=0),
            executable=sys.executable,
        ),
    )

    patch_required = check_profile(
        ProjectProfile(schema_version=1, project="sample", runtimes={"python": ">=3.12.1"}),
        tmp_path,
    )
    minor_required = check_profile(
        ProjectProfile(schema_version=1, project="sample", runtimes={"python": ">=3.12"}),
        tmp_path,
    )

    assert any(item.code == "PYTHON_VERSION_TOO_OLD" for item in patch_required)
    assert any(item.code == "PYTHON_VERSION_OK" for item in minor_required)


def test_project_interpreter_is_distinguished_from_devdock_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.12"\n', encoding="utf-8"
    )

    findings = check_profile(ProjectProfile(schema_version=1, project="sample"), tmp_path)

    project_finding = next(item for item in findings if item.code == "PROJECT_PYTHON_METADATA")
    assert project_finding.severity == "WARN"
    assert any("target not inspected" in item for item in project_finding.evidence)


def test_active_project_virtual_environment_reports_both_interpreter_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.12"\n', encoding="utf-8"
    )
    executable = tmp_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    monkeypatch.setattr(
        diagnostics,
        "sys",
        SimpleNamespace(
            version_info=SimpleNamespace(major=3, minor=12, micro=4),
            executable=str(executable),
        ),
    )

    findings = check_profile(ProjectProfile(schema_version=1, project="sample"), tmp_path)

    project_finding = next(item for item in findings if item.code == "PROJECT_PYTHON_METADATA")
    assert project_finding.severity == "PASS"
    assert any(item.startswith("DevDock executable:") for item in project_finding.evidence)
    assert any(item.startswith("Target project executable:") for item in project_finding.evidence)
    assert any(item == "Target project version: 3.12.4" for item in project_finding.evidence)


def test_environment_diagnostics_distinguish_unset_and_empty_without_values(
    tmp_path: Path,
) -> None:
    profile = ProjectProfile(
        schema_version=1,
        project="sample",
        required_env=["MISSING_VALUE", "EMPTY_VALUE", "SECRET_VALUE"],
    )

    findings = check_profile(
        profile,
        tmp_path,
        environ={"EMPTY_VALUE": "", "SECRET_VALUE": "super-secret"},
    )

    by_code = {item.code: item for item in findings}
    assert by_code["ENVIRONMENT_VARIABLE_UNSET"].summary.endswith("is unset.")
    assert by_code["ENVIRONMENT_VARIABLE_EMPTY"].summary.endswith("is set but empty.")
    assert "super-secret" not in repr(findings)


def test_required_files_and_directories_are_checked_and_symlinks_cannot_escape(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "present.txt").write_text("ok", encoding="utf-8")
    (tmp_path / "data").mkdir()
    try:
        (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Creating directory symlinks is not permitted here: {exc}")
    profile = ProjectProfile(
        schema_version=1,
        project="sample",
        required_files=[Path("present.txt"), Path("missing.txt")],
        required_dirs=[Path("data"), Path("escape")],
    )

    findings = check_profile(profile, tmp_path)
    codes = {item.code for item in findings}

    assert "REQUIRED_FILE_FOUND" in codes
    assert "REQUIRED_FILE_MISSING" in codes
    assert "REQUIRED_DIRECTORY_FOUND" in codes
    assert "REQUIRED_DIRECTORY_OUTSIDE_PROJECT" in codes


def test_rejects_resolved_paths_outside_project_without_platform_symlink_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path.parent / "outside-project"
    profile = ProjectProfile(schema_version=1, project="sample", required_dirs=[Path("escape")])
    original_resolve = Path.resolve

    def resolve_escape(path: Path, *args: object, **kwargs: object) -> Path:
        if path == tmp_path / "escape":
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_escape)

    findings = check_profile(profile, tmp_path)

    assert any(item.code == "REQUIRED_DIRECTORY_OUTSIDE_PROJECT" for item in findings)


def test_unresolvable_required_paths_are_reported_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = ProjectProfile(schema_version=1, project="sample", required_files=[Path("loop")])
    original_resolve = Path.resolve

    def fail_loop(path: Path, *args: object, **kwargs: object) -> Path:
        if path == tmp_path / "loop":
            raise RuntimeError("symlink loop")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_loop)

    findings = check_profile(profile, tmp_path)

    assert any(item.code == "REQUIRED_FILE_UNRESOLVABLE" for item in findings)


def test_compose_file_symlink_cannot_escape_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services: {}\n", encoding="utf-8")
    profile = ProjectProfile(
        schema_version=1,
        project="sample",
        runtimes={"docker": "compose"},
        compose_file=Path("compose.yaml"),
    )
    outside = tmp_path.parent / "external-compose.yaml"
    original_resolve = Path.resolve

    def resolve_escape(path: Path, *args: object, **kwargs: object) -> Path:
        if path == compose_path:
            return outside
        return original_resolve(path, *args, **kwargs)

    calls = 0

    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(Path, "resolve", resolve_escape)
    monkeypatch.setattr(compose.shutil, "which", lambda _: "docker-test")
    monkeypatch.setattr(compose.subprocess, "run", fake_run)

    findings = check_profile(profile, tmp_path)

    assert any(item.code == "COMPOSE_FILE_OUTSIDE_PROJECT" for item in findings)
    assert calls == 2  # CLI/plugin and daemon only; the external Compose file is never parsed.


def test_port_collision_reports_actionable_finding(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        profile = ProjectProfile(schema_version=1, project="sample", ports=[port])

        findings = check_profile(profile, tmp_path)

    collision = next(item for item in findings if item.code == "PORT_UNAVAILABLE")
    assert f"{port}" in collision.summary
    assert collision.remedy


def test_compose_cli_daemon_and_parser_are_checked_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    profile = ProjectProfile(
        schema_version=1,
        project="sample",
        runtimes={"docker": "compose"},
        compose_file=Path("compose.yaml"),
    )
    results = iter(
        [
            SimpleNamespace(returncode=0, stdout="compose-output-secret", stderr=""),
            SimpleNamespace(returncode=0, stdout="daemon-output-secret", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(compose.shutil, "which", lambda _: "docker-test")

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        calls.append(command)
        return next(results)

    monkeypatch.setattr(compose.subprocess, "run", fake_run)

    findings = check_profile(profile, tmp_path)
    codes = {item.code for item in findings}

    assert {
        "COMPOSE_CLI_AVAILABLE",
        "DOCKER_DAEMON_AVAILABLE",
        "COMPOSE_CONFIG_VALID",
    } <= codes
    assert len(calls) == 3
    assert calls[2][2:5] == ["-f", str(compose_file), "config"]
    assert "output-secret" not in repr(findings)


def test_compose_parser_errors_do_not_echo_interpolated_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    profile = ProjectProfile(
        schema_version=1,
        project="sample",
        runtimes={"docker": "compose"},
        compose_file=Path("compose.yaml"),
    )
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="v2", stderr=""),
            SimpleNamespace(returncode=0, stdout="27.0", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="TOKEN=secret-value"),
        ]
    )
    monkeypatch.setattr(compose.shutil, "which", lambda _: "docker-test")
    monkeypatch.setattr(compose.subprocess, "run", lambda *_args, **_kwargs: next(responses))

    findings = check_profile(profile, tmp_path)
    invalid = next(item for item in findings if item.code == "COMPOSE_CONFIG_INVALID")

    assert "secret-value" not in repr(invalid)
    assert "interpolated values" in invalid.summary


@pytest.mark.parametrize(
    ("docker_path", "compose_result", "expected_code"),
    [
        (None, None, "DOCKER_CLI_MISSING"),
        (
            "docker-test",
            SimpleNamespace(returncode=1, stdout="", stderr="plugin error"),
            "COMPOSE_CLI_UNAVAILABLE",
        ),
    ],
)
def test_compose_reports_missing_cli_or_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    docker_path: str | None,
    compose_result: SimpleNamespace | None,
    expected_code: str,
) -> None:
    profile = ProjectProfile(schema_version=1, project="sample", runtimes={"docker": "compose"})
    monkeypatch.setattr(compose.shutil, "which", lambda _: docker_path)
    if compose_result is not None:
        monkeypatch.setattr(compose.subprocess, "run", lambda *_args, **_kwargs: compose_result)

    findings = check_profile(profile, tmp_path)

    assert any(item.code == expected_code and item.severity == "FAIL" for item in findings)
    assert any(item.code == "DOCKER_DAEMON_SKIPPED" for item in findings)
    assert any(item.code == "COMPOSE_CONFIG_SKIPPED" for item in findings)


def test_compose_parser_is_checked_even_when_daemon_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    profile = ProjectProfile(
        schema_version=1,
        project="sample",
        runtimes={"docker": "compose"},
        compose_file=Path("compose.yaml"),
    )
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="v2", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="daemon is stopped"),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
    )
    monkeypatch.setattr(compose.shutil, "which", lambda _: "docker-test")
    monkeypatch.setattr(compose.subprocess, "run", lambda *_args, **_kwargs: next(responses))

    findings = check_profile(profile, tmp_path)
    codes = {item.code for item in findings}

    assert "DOCKER_DAEMON_UNAVAILABLE" in codes
    assert "COMPOSE_CONFIG_VALID" in codes
