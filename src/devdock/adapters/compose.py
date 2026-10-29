"""Docker Compose adapter built on argument arrays and bounded subprocesses."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from devdock.adapters.base import (
    CommandResult,
    CommandRunner,
    ComposeCapabilities,
    ConfigValidation,
    ContainerRecord,
    DaemonState,
    NetworkRecord,
    ResourceInventory,
)

VERSION_TIMEOUT = 5.0
DAEMON_TIMEOUT = 5.0
CONFIG_TIMEOUT = 10.0
LIST_TIMEOUT = 10.0

PROJECT_LABEL = "com.docker.compose.project"
_FIELD_SEPARATOR = "\t"
# One Go template field per ContainerRecord attribute, in order. ``.Label`` yields
# an empty string for absent labels, so every line has the same field count.
_CONTAINER_FORMAT = _FIELD_SEPARATOR.join(
    (
        "{{.ID}}",
        "{{.Names}}",
        "{{.State}}",
        f'{{{{.Label "{PROJECT_LABEL}"}}}}',
        '{{.Label "com.docker.compose.project.working_dir"}}',
        '{{.Label "com.docker.compose.project.config_files"}}',
        '{{.Label "com.docker.compose.service"}}',
        '{{.Label "com.docker.compose.oneoff"}}',
    )
)
_CONTAINER_FIELD_COUNT = 8
_NETWORK_FORMAT = _FIELD_SEPARATOR.join(("{{.ID}}", "{{.Name}}"))
_VOLUME_FORMAT = "{{.Name}}"
_SAFE_PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


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

    def list_project_resources(self, project_name: str) -> ResourceInventory:
        """Query containers (by label and by name prefix), networks, and volumes.

        Only identity fields and Compose ownership labels are requested from the
        runtime; container environment and full label sets are never read.
        ``project_name`` must already be Compose-safe so it is inert inside
        filter expressions.
        """

        if not _SAFE_PROJECT_NAME.fullmatch(project_name):
            raise ValueError(f"project name is not filter-safe: {project_name!r}")
        executable = self._locate("docker")
        if executable is None:
            return ResourceInventory(project_name, errors=("Docker CLI is not available.",))
        label_filter = f"label={PROJECT_LABEL}={project_name}"
        errors: list[str] = []

        def query(description: str, args: list[str]) -> list[str]:
            try:
                result = self._runner([executable, *args], timeout=LIST_TIMEOUT)
            except (OSError, subprocess.TimeoutExpired):
                errors.append(f"{description} did not complete.")
                return []
            if result.returncode:
                errors.append(f"{description} failed with exit code {result.returncode}.")
                return []
            return [line for line in result.stdout.splitlines() if line.strip()]

        containers = tuple(
            _parse_container(line)
            for line in query(
                "Container query by project label",
                ["ps", "--all", "--no-trunc", "--filter", label_filter,
                 "--format", _CONTAINER_FORMAT],
            )
        )
        collisions = tuple(
            _parse_container(line)
            for line in query(
                "Container query by name prefix",
                ["ps", "--all", "--no-trunc", "--filter", f"name=^{project_name}-",
                 "--format", _CONTAINER_FORMAT],
            )
        )
        networks = tuple(
            _parse_network(line)
            for line in query(
                "Network query", ["network", "ls", "--filter", label_filter,
                                  "--format", _NETWORK_FORMAT],
            )
        )
        volumes = tuple(
            line.strip()
            for line in query(
                "Volume query", ["volume", "ls", "--filter", label_filter,
                                 "--format", _VOLUME_FORMAT],
            )
        )
        return ResourceInventory(
            project_name,
            containers=containers,
            name_collisions=collisions,
            networks=tuple(item for item in networks if item is not None),
            volumes=volumes,
            errors=tuple(errors),
        )


def _parse_container(line: str) -> ContainerRecord:
    fields = line.split(_FIELD_SEPARATOR)
    if len(fields) != _CONTAINER_FIELD_COUNT:
        return ContainerRecord(
            id="",
            name=fields[1] if len(fields) > 1 else "",
            state="",
            parse_error=f"Expected {_CONTAINER_FIELD_COUNT} fields, got {len(fields)}.",
        )
    identifier, name, state, project, working_dir, config_files, service, oneoff = fields
    return ContainerRecord(
        id=identifier,
        name=name,
        state=state,
        project=project,
        working_dir=working_dir,
        config_files=config_files,
        service=service,
        oneoff=oneoff,
    )


def _parse_network(line: str) -> NetworkRecord | None:
    fields = line.split(_FIELD_SEPARATOR)
    if len(fields) != 2:
        return None
    return NetworkRecord(id=fields[0], name=fields[1])
