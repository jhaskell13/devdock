"""Read-only local prerequisite checks with stable, actionable findings."""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from devdock.config import ProjectProfile
from devdock.discovery import COMPOSE_NAMES
from devdock.metadata import read_python_metadata

PYTHON_REQUIREMENT = re.compile(r"^>=\s*(\d+)\.(\d+)(?:\.(\d+))?$")
Severity = Literal["PASS", "WARN", "FAIL"]


@dataclass(frozen=True)
class Finding:
    """One stable diagnostic result; evidence is safe for terminal display."""

    code: str
    severity: Severity
    summary: str
    evidence: tuple[str, ...] = ()
    remedy: str | None = None
    doc_ref: str | None = None

    @property
    def level(self) -> Severity:
        """Compatibility name used by the current CLI renderer."""

        return self.severity

    @property
    def message(self) -> str:
        """Compatibility name used by callers expecting the old finding shape."""

        return self.summary


def _finding(
    code: str,
    severity: Severity,
    summary: str,
    *,
    evidence: tuple[str, ...] = (),
    remedy: str | None = None,
) -> Finding:
    return Finding(code, severity, summary, evidence, remedy, "docs/cli.md#diagnostic-codes")


def _compose_path(profile: ProjectProfile, root: Path) -> Path | None:
    if profile.compose_file is not None:
        return root / profile.compose_file
    return next((root / name for name in COMPOSE_NAMES if (root / name).is_file()), None)


def _inside_project(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def _check_compose(profile: ProjectProfile, root: Path, findings: list[Finding]) -> None:
    requirement = profile.runtimes.get("docker")
    if requirement is None:
        return
    if requirement.strip().lower() != "compose":
        findings.append(
            _finding(
                "DOCKER_REQUIREMENT_UNSUPPORTED",
                "FAIL",
                f"Unsupported Docker requirement: {requirement}",
                remedy="Use 'compose' for the initial Docker Compose adapter.",
            )
        )
        return

    executable = shutil.which("docker")
    if executable is None:
        findings.append(
            _finding(
                "DOCKER_CLI_MISSING",
                "FAIL",
                "Docker CLI is not available on PATH.",
                remedy="Install Docker Desktop or Docker Engine with the Compose plugin.",
            )
        )
        return

    try:
        version = subprocess.run(
            [executable, "compose", "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        findings.append(
            _finding(
                "COMPOSE_CLI_UNAVAILABLE",
                "FAIL",
                "Could not query the Docker Compose plugin.",
                evidence=(f"Executable: {executable}",),
                remedy="Install or enable the Docker Compose plugin, then retry.",
            )
        )
        return
    if version.returncode:
        findings.append(
            _finding(
                "COMPOSE_CLI_UNAVAILABLE",
                "FAIL",
                "Docker CLI is present but its Compose plugin did not respond successfully.",
                evidence=(f"Executable: {executable}", f"Exit code: {version.returncode}"),
                remedy="Install or enable the Docker Compose plugin, then retry.",
            )
        )
        return
    findings.append(
        _finding(
            "COMPOSE_CLI_AVAILABLE",
            "PASS",
            "Docker Compose CLI is available.",
            evidence=(
                f"Executable: {executable}",
                version.stdout.strip() or "Version command succeeded.",
            ),
        )
    )

    try:
        daemon = subprocess.run(
            [executable, "info", "--format", "{{.ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        daemon = None
    if daemon is None or daemon.returncode:
        findings.append(
            _finding(
                "DOCKER_DAEMON_UNAVAILABLE",
                "FAIL",
                "Docker CLI is available, but the daemon did not respond.",
                remedy="Start Docker Desktop or the Docker Engine service and retry.",
            )
        )
    else:
        findings.append(
            _finding(
                "DOCKER_DAEMON_AVAILABLE",
                "PASS",
                "Docker daemon is reachable.",
                evidence=(f"Server version: {daemon.stdout.strip() or 'reported'}",),
            )
        )

    compose_path = _compose_path(profile, root)
    if compose_path is None:
        findings.append(
            _finding(
                "COMPOSE_FILE_MISSING",
                "FAIL",
                "No Compose file was configured or discovered.",
                remedy="Set compose_file in devdock.yaml or add a common Compose filename.",
            )
        )
        return
    if not _inside_project(compose_path, root):
        findings.append(
            _finding(
                "COMPOSE_FILE_OUTSIDE_PROJECT",
                "FAIL",
                "Compose file resolves outside the selected project.",
                evidence=(f"File: {compose_path}",),
                remedy="Remove the escaping symlink or choose a project-contained Compose file.",
            )
        )
        return
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
        return
    try:
        parsed = subprocess.run(
            [executable, "compose", "-f", str(compose_path), "config", "--quiet"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        parsed = None
    if parsed is None or parsed.returncode:
        code = "COMPOSE_CONFIG_UNAVAILABLE" if parsed is None else "COMPOSE_CONFIG_INVALID"
        evidence = (f"File: {compose_path}",) if parsed is None else (
            f"File: {compose_path}", f"Exit code: {parsed.returncode}",
        )
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
    else:
        findings.append(
            _finding(
                "COMPOSE_CONFIG_VALID",
                "PASS",
                "Compose configuration parsed successfully.",
                evidence=(f"File: {compose_path}",),
            )
        )


def check_profile(
    profile: ProjectProfile,
    root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[Finding]:
    """Return actionable diagnostics without changing the project or host."""

    env = os.environ if environ is None else environ
    findings: list[Finding] = [
        _finding("PROFILE_VALID", "PASS", f"Profile is valid for {profile.project}")
    ]

    project_python = read_python_metadata(root)
    if project_python.declarations:
        declarations = tuple(
            f"{item.source}: {item.value}" for item in project_python.declarations
        )
        current_executable = Path(sys.executable).resolve()
        local_venv_interpreters = (
            (root / ".venv" / "Scripts" / "python.exe").resolve(),
            (root / ".venv" / "bin" / "python").resolve(),
        )
        selected = current_executable in local_venv_interpreters
        interpreter_evidence = (
            "Project interpreter version: "
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        ) if selected else (
            "DevDock interpreter version: "
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} "
            "(target interpreter not inspected)",
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
                evidence=declarations + (f"DevDock executable: {current_executable}",)
                + interpreter_evidence,
                remedy=None
                if selected
                else "Activate the project's .venv and rerun DevDock to check that interpreter.",
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
                        f"Unsupported Python requirement: {requirement}",
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

    _check_compose(profile, root, findings)

    if profile.compose_file is not None and "docker" not in profile.runtimes:
        compose_path = root / profile.compose_file
        if not _inside_project(compose_path, root):
            findings.append(
                _finding(
                    "COMPOSE_FILE_OUTSIDE_PROJECT",
                    "FAIL",
                    "Compose file resolves outside the selected project.",
                    evidence=(f"File: {compose_path}",),
                    remedy="Remove the escaping symlink or choose a project-contained "
                    "Compose file.",
                )
            )
        elif compose_path.is_file():
            findings.append(
                _finding(
                    "COMPOSE_FILE_FOUND",
                    "PASS",
                    f"Compose file found: {profile.compose_file}",
                    evidence=(f"Path: {compose_path}",),
                )
            )
        else:
            findings.append(
                _finding(
                    "COMPOSE_FILE_MISSING",
                    "FAIL",
                    f"Compose file not found: {profile.compose_file}",
                    remedy="Create the file or update compose_file in devdock.yaml.",
                )
            )

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
        target = (root / required_file).resolve()
        if not target.is_relative_to(root.resolve()):
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
                _finding(
                    "REQUIRED_FILE_FOUND", "PASS", f"Required file found: {required_file}"
                )
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
        target = (root / required_dir).resolve()
        if not target.is_relative_to(root.resolve()):
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
