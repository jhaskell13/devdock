"""Read-only local prerequisite checks for a project profile."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from devdock.config import ProjectProfile

PYTHON_REQUIREMENT = re.compile(r"^>=\s*(\d+)\.(\d+)(?:\.\d+)?$")


@dataclass(frozen=True)
class Finding:
    level: str
    message: str
    remedy: str | None = None


def check_profile(profile: ProjectProfile, root: Path) -> list[Finding]:
    """Return actionable findings without changing the user's environment."""

    findings: list[Finding] = [Finding("PASS", f"Profile is valid for {profile.project}")]

    for runtime, requirement in profile.runtimes.items():
        if runtime == "python":
            match = PYTHON_REQUIREMENT.fullmatch(requirement.strip())
            if match is None:
                findings.append(
                    Finding(
                        "FAIL",
                        f"Unsupported Python requirement: {requirement}",
                        "Use a minimum version such as '>=3.12'.",
                    )
                )
                continue
            required = (int(match.group(1)), int(match.group(2)))
            current = sys.version_info[:2]
            if current < required:
                findings.append(
                    Finding(
                        "FAIL",
                        f"Python {required[0]}.{required[1]} or newer is required; "
                        f"found {sys.version_info.major}.{sys.version_info.minor}.",
                        "Install the required Python version and rerun DevDock.",
                    )
                )
            else:
                findings.append(
                    Finding(
                        "PASS",
                        f"Python {sys.version_info.major}.{sys.version_info.minor} "
                        "meets the declared minimum.",
                    )
                )
        elif runtime == "docker":
            if requirement.strip().lower() != "compose":
                findings.append(
                    Finding(
                        "FAIL",
                        f"Unsupported Docker requirement: {requirement}",
                        "Use 'compose' for the initial Docker Compose adapter.",
                    )
                )
                continue
            executable = shutil.which("docker")
            if executable is None:
                findings.append(
                    Finding(
                        "FAIL",
                        "Docker CLI is not available on PATH.",
                        "Install Docker Desktop or Docker Engine with the Compose plugin.",
                    )
                )
                continue
            try:
                result = subprocess.run(
                    [executable, "compose", "version"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                findings.append(Finding("FAIL", f"Could not query Docker Compose: {exc}"))
                continue
            if result.returncode:
                details = result.stderr.strip() or "Docker Compose command failed."
                findings.append(
                    Finding(
                        "FAIL",
                        details,
                        "Install or enable the Docker Compose plugin, then retry.",
                    )
                )
            else:
                findings.append(Finding("PASS", result.stdout.strip() or "Docker Compose is available."))
        else:
            findings.append(
                Finding(
                    "FAIL",
                    f"Runtime {runtime!r} is not supported yet.",
                    "Remove it from runtimes or use a currently supported runtime.",
                )
            )

    if profile.compose_file is not None:
        compose_path = root / profile.compose_file
        if compose_path.is_file():
            findings.append(Finding("PASS", f"Compose file found: {profile.compose_file}"))
        else:
            findings.append(
                Finding(
                    "FAIL",
                    f"Compose file not found: {profile.compose_file}",
                    "Create the file or update compose_file in devdock.yaml.",
                )
            )

    for name in profile.required_env:
        if os.environ.get(name):
            findings.append(Finding("PASS", f"Required environment variable {name} is set."))
        else:
            findings.append(
                Finding(
                    "FAIL",
                    f"Required environment variable {name} is not set.",
                    f"Set {name} in your shell before running this command.",
                )
            )

    return findings

