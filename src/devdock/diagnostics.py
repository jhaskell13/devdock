"""Read-only local prerequisite checks with stable, actionable findings."""

from __future__ import annotations

import os
import re
import socket
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from devdock.adapters import ComposeAdapter, RuntimeAdapter
from devdock.config import ProjectProfile
from devdock.metadata import read_python_metadata
from devdock.ownership import compose_path_for, resolves_inside

PYTHON_REQUIREMENT = re.compile(r"^>=\s*(\d+)\.(\d+)(?:\.(\d+))?$")
Severity = Literal["PASS", "WARN", "FAIL", "SKIP"]


@dataclass(frozen=True)
class Finding:
    """One stable diagnostic result; evidence is safe for terminal display."""

    code: str
    severity: Severity
    summary: str
    evidence: tuple[str, ...] = ()
    remedy: str | None = None
    doc_ref: str | None = None


def _finding(
    code: str,
    severity: Severity,
    summary: str,
    *,
    evidence: tuple[str, ...] = (),
    remedy: str | None = None,
) -> Finding:
    return Finding(code, severity, summary, evidence, remedy, "docs/cli.md#diagnostic-codes")


def _compose_skipped(code: str, check_name: str, reason: str) -> Finding:
    return _finding(
        code,
        "SKIP",
        f"{check_name} was not run.",
        evidence=(reason,),
    )


def _resolved_path(path: Path) -> Path | None:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


def _project_python_interpreters(root: Path) -> tuple[Path, ...]:
    """Return conventional local virtual-environment interpreter paths."""

    names = (".venv", "venv", "env")
    candidates = [root / environment / "Scripts" / "python.exe" for environment in names]
    candidates.extend(root / environment / "bin" / "python" for environment in names)
    return tuple(Path(os.path.abspath(path)) for path in candidates)


def _check_compose_file(
    profile: ProjectProfile, root: Path, findings: list[Finding]
) -> Path | None:
    """Report the Compose file's presence and containment; return it only when usable."""

    compose_path = compose_path_for(profile, root)
    docker_declared = "docker" in profile.runtimes
    if compose_path is None:
        if docker_declared:
            findings.append(
                _finding(
                    "COMPOSE_FILE_MISSING",
                    "FAIL",
                    "No Compose file was configured or discovered.",
                    remedy="Set compose_file in devdock.yaml or add a common Compose filename.",
                )
            )
        return None
    if not resolves_inside(compose_path, root):
        findings.append(
            _finding(
                "COMPOSE_FILE_OUTSIDE_PROJECT",
                "FAIL",
                "Compose file does not resolve inside the selected project.",
                evidence=(f"File: {compose_path}",),
                remedy="Remove the escaping symlink or choose a project-contained Compose file.",
            )
        )
        return None
    if not compose_path.is_file():
        findings.append(
            _finding(
                "COMPOSE_FILE_MISSING",
                "FAIL",
                f"Compose file not found: {compose_path.name}",
                evidence=(f"Expected path: {compose_path}",),
                remedy="Create the file or update compose_file in devdock.yaml.",
            )
        )
        return None
    findings.append(
        _finding(
            "COMPOSE_FILE_FOUND",
            "PASS",
            f"Compose file found: {compose_path.name}",
            evidence=(f"Path: {compose_path}",),
        )
    )
    return compose_path


def _check_compose(
    profile: ProjectProfile,
    root: Path,
    findings: list[Finding],
    adapter: RuntimeAdapter,
    compose_path: Path | None,
) -> None:
    requirement = profile.runtimes.get("docker")
    if requirement is None:
        return
    if requirement.strip().lower() != "compose":
        findings.append(
            _finding(
                "DOCKER_REQUIREMENT_UNSUPPORTED",
                "FAIL",
                "Unsupported Docker requirement.",
                remedy="Use 'compose' for the initial Docker Compose adapter.",
            )
        )
        findings.append(
            _compose_skipped(
                "DOCKER_CHECKS_SKIPPED",
                "Docker checks",
                "The configured Docker requirement is unsupported.",
            )
        )
        return

    capabilities = adapter.detect_capabilities()
    executable = capabilities.executable
    if executable is None:
        findings.append(
            _finding(
                "DOCKER_CLI_MISSING",
                "FAIL",
                "Docker CLI is not available on PATH.",
                remedy="Install Docker Desktop or Docker Engine with the Compose plugin.",
            )
        )
        findings.append(
            _compose_skipped(
                "DOCKER_DAEMON_SKIPPED", "Docker daemon check", "Docker CLI is missing."
            )
        )
        findings.append(
            _compose_skipped(
                "COMPOSE_CONFIG_SKIPPED", "Compose config check", "Docker CLI is missing."
            )
        )
        return

    if not capabilities.compose_available:
        if capabilities.compose_cli == "unresponsive":
            summary = "Could not query the Docker Compose plugin."
            evidence: tuple[str, ...] = (f"Executable: {executable}",)
        else:
            summary = "Docker CLI is present but its Compose plugin did not respond successfully."
            evidence = (
                f"Executable: {executable}",
                f"Exit code: {capabilities.compose_cli_returncode}",
            )
        findings.append(
            _finding(
                "COMPOSE_CLI_UNAVAILABLE",
                "FAIL",
                summary,
                evidence=evidence,
                remedy="Install or enable the Docker Compose plugin, then retry.",
            )
        )
        findings.append(
            _compose_skipped(
                "DOCKER_DAEMON_SKIPPED",
                "Docker daemon check",
                "The Compose CLI/plugin did not respond.",
            )
        )
        findings.append(
            _compose_skipped(
                "COMPOSE_CONFIG_SKIPPED",
                "Compose config check",
                "The Compose CLI/plugin did not respond.",
            )
        )
        return
    findings.append(
        _finding(
            "COMPOSE_CLI_AVAILABLE",
            "PASS",
            "Docker Compose CLI is available.",
            evidence=(f"Executable: {executable}", "Version command succeeded."),
        )
    )

    if capabilities.daemon_available:
        findings.append(
            _finding(
                "DOCKER_DAEMON_AVAILABLE",
                "PASS",
                "Docker daemon is reachable.",
                evidence=("Docker info request succeeded.",),
            )
        )
    else:
        findings.append(
            _finding(
                "DOCKER_DAEMON_UNAVAILABLE",
                "FAIL",
                "Docker CLI is available, but the daemon did not respond.",
                remedy="Start Docker Desktop or the Docker Engine service and retry.",
            )
        )

    if compose_path is None:
        findings.append(
            _compose_skipped(
                "COMPOSE_CONFIG_SKIPPED",
                "Compose config check",
                "There is no safe, existing Compose file to validate.",
            )
        )
        return
    validation = adapter.validate_config(compose_path, project_root=root)
    if validation.valid:
        findings.append(
            _finding(
                "COMPOSE_CONFIG_VALID",
                "PASS",
                "Compose configuration parsed successfully.",
                evidence=(f"File: {compose_path}",),
            )
        )
        return
    if validation.outcome == "unavailable":
        code = "COMPOSE_CONFIG_UNAVAILABLE"
        evidence = (f"File: {compose_path}",)
    else:
        code = "COMPOSE_CONFIG_INVALID"
        evidence = (f"File: {compose_path}", f"Exit code: {validation.returncode}")
    findings.append(
        _finding(
            code,
            "FAIL",
            "Compose configuration could not be validated. Output is suppressed to avoid "
            "exposing interpolated values.",
            evidence=evidence,
            remedy="Run 'docker compose config' locally to inspect the validation error, "
            "then correct the file.",
        )
    )


def check_profile(
    profile: ProjectProfile,
    root: Path,
    *,
    environ: Mapping[str, str] | None = None,
    adapter: RuntimeAdapter | None = None,
) -> list[Finding]:
    """Return actionable diagnostics without changing the project or host.

    ``adapter`` defaults to the local Docker Compose adapter; tests pass a fake.
    """

    env = os.environ if environ is None else environ
    compose_adapter = ComposeAdapter() if adapter is None else adapter
    findings: list[Finding] = [
        _finding("PROFILE_VALID", "PASS", f"Profile is valid for {profile.project}")
    ]

    project_python = read_python_metadata(root)
    if project_python.declarations:
        declarations = tuple(f"{item.source}: {item.value}" for item in project_python.declarations)
        current_executable = Path(os.path.abspath(sys.executable))
        candidates = _project_python_interpreters(root)
        selected = current_executable in candidates
        version_text = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        interpreter_evidence: tuple[str, ...]
        if selected:
            interpreter_evidence = (
                f"Target project executable: {current_executable}",
                f"Target project version: {version_text}",
            )
        else:
            configured_candidates = tuple(
                f"Conventional interpreter candidate: {candidate}"
                for candidate in candidates
                if candidate.is_file()
            )
            interpreter_evidence = (
                f"DevDock interpreter version: {version_text} (target not inspected)",
                *configured_candidates,
            )
        findings.append(
            _finding(
                "PROJECT_PYTHON_METADATA",
                "PASS" if selected else "WARN",
                "Project Python metadata was found; "
                + (
                    "DevDock is running from this project's .venv."
                    if selected
                    else "the target project's interpreter is not the interpreter running DevDock."
                ),
                evidence=declarations
                + (f"DevDock executable: {current_executable}",)
                + interpreter_evidence,
                remedy=None
                if selected
                else "Activate the project's .venv, venv, or env and rerun DevDock to inspect it.",
            )
        )
    for error in project_python.errors:
        findings.append(
            _finding(
                "PROJECT_PYTHON_METADATA_INVALID",
                "WARN",
                "Could not read a recognized Python metadata file.",
                evidence=(error,),
                remedy="Correct the metadata file encoding or syntax, or remove the invalid "
                "declaration.",
            )
        )

    for runtime, requirement in profile.runtimes.items():
        if runtime == "python":
            match = PYTHON_REQUIREMENT.fullmatch(requirement.strip())
            if match is None:
                findings.append(
                    _finding(
                        "PYTHON_REQUIREMENT_UNSUPPORTED",
                        "FAIL",
                        "Unsupported Python requirement.",
                        remedy="Use a minimum version such as '>=3.12'.",
                    )
                )
                continue
            required = (
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3) or 0),
            )
            current = (
                sys.version_info.major,
                sys.version_info.minor,
                sys.version_info.micro,
            )
            interpreter = str(Path(sys.executable).resolve())
            evidence = (
                f"Executable: {interpreter}",
                f"Inspected version: {current[0]}.{current[1]}.{current[2]}",
            )
            if current < required:
                findings.append(
                    _finding(
                        "PYTHON_VERSION_TOO_OLD",
                        "FAIL",
                        f"Python {required[0]}.{required[1]}.{required[2]} or newer is required; "
                        f"found {current[0]}.{current[1]}.{current[2]}.",
                        evidence=evidence,
                        remedy="Install the required Python version and rerun DevDock.",
                    )
                )
            else:
                findings.append(
                    _finding(
                        "PYTHON_VERSION_OK",
                        "PASS",
                        f"Python {current[0]}.{current[1]}.{current[2]} meets "
                        "the declared minimum.",
                        evidence=evidence,
                    )
                )
        elif runtime != "docker":
            findings.append(
                _finding(
                    "RUNTIME_UNSUPPORTED",
                    "FAIL",
                    f"Runtime {runtime!r} is not supported yet.",
                    remedy="Remove it from runtimes or use a currently supported runtime.",
                )
            )

    compose_path = _check_compose_file(profile, root, findings)
    _check_compose(profile, root, findings, compose_adapter, compose_path)

    for name in profile.required_env:
        if name not in env:
            findings.append(
                _finding(
                    "ENVIRONMENT_VARIABLE_UNSET",
                    "FAIL",
                    f"Required environment variable {name} is unset.",
                    remedy=f"Set {name} in your shell before running this command.",
                )
            )
        elif env[name] == "":
            findings.append(
                _finding(
                    "ENVIRONMENT_VARIABLE_EMPTY",
                    "FAIL",
                    f"Required environment variable {name} is set but empty.",
                    remedy=f"Set a non-empty value for {name} in your shell.",
                )
            )
        else:
            findings.append(
                _finding(
                    "ENVIRONMENT_VARIABLE_SET",
                    "PASS",
                    f"Required environment variable {name} is set.",
                    evidence=("Value redacted.",),
                )
            )

    for required_file in profile.required_files:
        target = _resolved_path(root / required_file)
        project_root = _resolved_path(root)
        if target is None or project_root is None:
            findings.append(
                _finding(
                    "REQUIRED_FILE_UNRESOLVABLE",
                    "FAIL",
                    f"Required file path could not be resolved: {required_file}",
                    remedy="Check for broken or looping symlinks in the required file path.",
                )
            )
        elif not target.is_relative_to(project_root):
            findings.append(
                _finding(
                    "REQUIRED_FILE_OUTSIDE_PROJECT",
                    "FAIL",
                    f"Required file resolves outside the project: {required_file}",
                    remedy="Remove the escaping symlink or choose a project-contained path.",
                )
            )
        elif target.is_file():
            findings.append(
                _finding("REQUIRED_FILE_FOUND", "PASS", f"Required file found: {required_file}")
            )
        else:
            findings.append(
                _finding(
                    "REQUIRED_FILE_MISSING",
                    "FAIL",
                    f"Required file not found: {required_file}",
                    remedy=f"Create {required_file} or remove it from required_files.",
                )
            )

    for required_dir in profile.required_dirs:
        target = _resolved_path(root / required_dir)
        project_root = _resolved_path(root)
        if target is None or project_root is None:
            findings.append(
                _finding(
                    "REQUIRED_DIRECTORY_UNRESOLVABLE",
                    "FAIL",
                    f"Required directory path could not be resolved: {required_dir}",
                    remedy="Check for broken or looping symlinks in the required directory path.",
                )
            )
        elif not target.is_relative_to(project_root):
            findings.append(
                _finding(
                    "REQUIRED_DIRECTORY_OUTSIDE_PROJECT",
                    "FAIL",
                    f"Required directory resolves outside the project: {required_dir}",
                    remedy="Remove the escaping symlink or choose a project-contained path.",
                )
            )
        elif target.is_dir():
            findings.append(
                _finding(
                    "REQUIRED_DIRECTORY_FOUND", "PASS", f"Required directory found: {required_dir}"
                )
            )
        else:
            findings.append(
                _finding(
                    "REQUIRED_DIRECTORY_MISSING",
                    "FAIL",
                    f"Required directory not found: {required_dir}",
                    remedy=f"Create {required_dir} or remove it from required_dirs.",
                )
            )

    for port in profile.ports:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                listener.bind(("127.0.0.1", port))
        except OSError:
            findings.append(
                _finding(
                    "PORT_UNAVAILABLE",
                    "FAIL",
                    f"TCP port {port} is unavailable on 127.0.0.1.",
                    remedy=f"Stop the process using port {port} or configure another port.",
                )
            )
        else:
            findings.append(
                _finding(
                    "PORT_AVAILABLE",
                    "PASS",
                    f"TCP port {port} appears available on 127.0.0.1.",
                    evidence=("Availability can change after this check.",),
                )
            )

    return findings
