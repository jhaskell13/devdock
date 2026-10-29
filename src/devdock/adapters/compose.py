"""Docker Compose adapter built on argument arrays and bounded subprocesses."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from devdock.adapters.base import (
    CommandResult,
    CommandRunner,
    ComposeCapabilities,
    ConfigValidation,
    DaemonState,
)

VERSION_TIMEOUT = 5.0
DAEMON_TIMEOUT = 5.0
CONFIG_TIMEOUT = 10.0


def run_command(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float,
) -> CommandResult:
    """Default runner: no shell, captured text output, enforced timeout."""

    completed = subprocess.run(
        list(args),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _locate_docker(name: str) -> str | None:
    return shutil.which(name)


class ComposeAdapter:
    """Local Docker Compose operations with injectable execution for tests."""

    def __init__(
        self,
        runner: CommandRunner = run_command,
        *,
        locate: Callable[[str], str | None] = _locate_docker,
    ) -> None:
        self._runner = runner
        self._locate = locate

    def detect_capabilities(self) -> ComposeCapabilities:
        """Check CLI presence, Compose plugin response, and daemon reachability.

        Checks stop at the first missing prerequisite so a later state is only
        reported when its prerequisite succeeded.
        """

        executable = self._locate("docker")
        if executable is None:
            return ComposeCapabilities(executable=None, compose_cli="missing")

        try:
            version = self._runner(
                [executable, "compose", "version"], timeout=VERSION_TIMEOUT
            )
        except (OSError, subprocess.TimeoutExpired):
            return ComposeCapabilities(executable=executable, compose_cli="unresponsive")
        if version.returncode:
            return ComposeCapabilities(
                executable=executable,
                compose_cli="failed",
                compose_cli_returncode=version.returncode,
            )

        daemon_state: DaemonState
        try:
            daemon = self._runner(
                [executable, "info", "--format", "{{.ServerVersion}}"],
                timeout=DAEMON_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            daemon_state = "unavailable"
        else:
            daemon_state = "unavailable" if daemon.returncode else "available"
        return ComposeCapabilities(
            executable=executable,
            compose_cli="available",
            compose_cli_returncode=0,
            daemon=daemon_state,
        )

    def validate_config(self, config_path: Path, *, project_root: Path) -> ConfigValidation:
        """Run ``docker compose config --quiet`` and report only the outcome."""

        executable = self._locate("docker")
        if executable is None:
            return ConfigValidation(config_path, "unavailable")
        try:
            parsed = self._runner(
                [executable, "compose", "-f", str(config_path), "config", "--quiet"],
                cwd=project_root,
                timeout=CONFIG_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ConfigValidation(config_path, "unavailable")
        if parsed.returncode:
            return ConfigValidation(config_path, "invalid", parsed.returncode)
        return ConfigValidation(config_path, "valid", 0)
