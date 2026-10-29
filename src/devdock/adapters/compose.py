"""Docker Compose adapter built on argument arrays and bounded subprocesses."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from devdock.adapters.base import (
    CommandResult,
    CommandRunner,
    ComposeCapabilities,
    ConfigValidation,
    ContainerRecord,
    DaemonState,
    DeclaredServices,
    NetworkRecord,
    OperationOutcome,
    PublishedPort,
    ResourceInventory,
    ServiceObservation,
    ServiceObservations,
)

VERSION_TIMEOUT = 5.0
DAEMON_TIMEOUT = 5.0
CONFIG_TIMEOUT = 10.0
LIST_TIMEOUT = 10.0
START_TIMEOUT = 300.0
STOP_TIMEOUT = 120.0

PROJECT_LABEL = "com.docker.compose.project"
_FIELD_SEPARATOR = "\t"


def _json_field(expression: str) -> str:
    """Wrap a template expression so its value is JSON-encoded.

    Label values are attacker-influenced and may contain tabs or newlines; JSON
    encoding escapes them, so a line always has exactly one field per column and
    a single container can never masquerade as two.
    """

    return "{{json (" + expression + ")}}"


# One field per ContainerRecord attribute, in order. ``.Label`` yields an empty
# string for absent labels, so every line has the same field count.
_CONTAINER_FORMAT = _FIELD_SEPARATOR.join(
    _json_field(expression)
    for expression in (
        ".ID",
        ".Names",
        ".State",
        f'.Label "{PROJECT_LABEL}"',
        '.Label "com.docker.compose.project.working_dir"',
        '.Label "com.docker.compose.project.config_files"',
        '.Label "com.docker.compose.service"',
        '.Label "com.docker.compose.oneoff"',
    )
)
_CONTAINER_FIELD_COUNT = 8
_NETWORK_FORMAT = _FIELD_SEPARATOR.join((_json_field(".ID"), _json_field(".Name")))
_VOLUME_FORMAT = _json_field(".Name")
# Compose-safe project names: also inert inside --filter expressions, so this
# is the single rule that ownership.py derives names against and the adapter
# re-checks before every invocation.
SAFE_PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Compose reads COMPOSE_* settings from the project's .env, which DevDock treats
# as untrusted. Shell environment beats .env in Compose's precedence, so pinning
# these here keeps the preview truthful regardless of what .env says.
HARDENED_ENVIRONMENT: Mapping[str, str] = {
    "COMPOSE_REMOVE_ORPHANS": "false",
    "COMPOSE_ANSI": "never",
    "COMPOSE_PROGRESS": "plain",
}


def run_command(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Default runner: no shell, captured text output, enforced timeout."""

    completed = subprocess.run(
        list(args),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=None if env is None else dict(env),
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

    def _environment(self) -> dict[str, str]:
        return {**os.environ, **HARDENED_ENVIRONMENT}

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
                [executable, "compose", "version"],
                timeout=VERSION_TIMEOUT,
                env=self._environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return ComposeCapabilities(executable=executable, compose_cli="unresponsive")
        if version.returncode:
            return ComposeCapabilities(
                executable=executable,
                compose_cli="failed",
                compose_cli_returncode=version.returncode,
            )

        # `docker info` exits 0 even when the daemon socket is unreachable, so it
        # cannot be used as a reachability probe. `docker version` exits non-zero
        # and prints nothing to stdout unless the server answered; require both.
        daemon_state: DaemonState
        try:
            daemon = self._runner(
                [executable, "version", "--format", "{{.Server.Version}}"],
                timeout=DAEMON_TIMEOUT,
                env=self._environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            daemon_state = "unavailable"
        else:
            daemon_state = _daemon_state(daemon)
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
                env=self._environment(),
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

        if not SAFE_PROJECT_NAME.fullmatch(project_name):
            raise ValueError(f"project name is not filter-safe: {project_name!r}")
        executable = self._locate("docker")
        if executable is None:
            return ResourceInventory(project_name, errors=("Docker CLI is not available.",))
        label_filter = f"label={PROJECT_LABEL}={project_name}"
        errors: list[str] = []

        def query(description: str, args: list[str]) -> list[str]:
            try:
                result = self._runner(
                    [executable, *args], timeout=LIST_TIMEOUT, env=self._environment()
                )
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
                [
                    "ps",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    label_filter,
                    "--format",
                    _CONTAINER_FORMAT,
                ],
            )
        )
        collisions = tuple(
            _parse_container(line)
            for line in query(
                "Container query by name prefix",
                [
                    "ps",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    f"name=^{project_name}-",
                    "--format",
                    _CONTAINER_FORMAT,
                ],
            )
        )
        networks = tuple(
            _parse_network(line)
            for line in query(
                "Network query",
                ["network", "ls", "--filter", label_filter, "--format", _NETWORK_FORMAT],
            )
        )
        volumes = tuple(
            _parse_volume(line)
            for line in query(
                "Volume query",
                ["volume", "ls", "--filter", label_filter, "--format", _VOLUME_FORMAT],
            )
        )
        return ResourceInventory(
            project_name,
            containers=containers,
            name_collisions=collisions,
            networks=tuple(item for item in networks if item is not None),
            volumes=tuple(item for item in volumes if item is not None),
            errors=tuple(errors),
        )

    def start(
        self,
        *,
        project_name: str,
        project_root: Path,
        compose_file: Path,
        timeout: float = START_TIMEOUT,
    ) -> OperationOutcome:
        """Run ``docker compose up --detach`` for exactly this project.

        DevDock supplies the project name, project directory, and Compose file
        itself; no user-provided Compose options are forwarded. The command
        returns once containers are created and started, which is not the same
        as being ready. Readiness waiting is deliberately absent here.
        """

        executable = self._locate("docker")
        if executable is None:
            return OperationOutcome("unavailable")
        args = self._project_args(executable, project_name, project_root, compose_file)
        args += ["up", "--detach", "--remove-orphans=false"]
        return self._run_operation(args, project_root, timeout)

    def _project_args(
        self, executable: str, project_name: str, project_root: Path, compose_file: Path
    ) -> list[str]:
        """Common ``docker compose`` prefix; rejects names that are unsafe in filters."""

        if not SAFE_PROJECT_NAME.fullmatch(project_name):
            raise ValueError(f"project name is not filter-safe: {project_name!r}")
        return [
            executable,
            "compose",
            "--project-directory",
            str(project_root),
            "--file",
            str(compose_file),
            "--project-name",
            project_name,
        ]

    def stop(
        self,
        *,
        project_name: str,
        project_root: Path,
        compose_file: Path,
        timeout: float = STOP_TIMEOUT,
    ) -> OperationOutcome:
        """Run ``docker compose down`` for exactly this project.

        Compose removes the project's containers and networks by default and
        leaves volumes and images alone unless asked; DevDock never asks. It
        also does not pass ``--remove-orphans``, so containers from services no
        longer in the Compose file are reported, not removed.
        """

        executable = self._locate("docker")
        if executable is None:
            return OperationOutcome("unavailable")
        args = self._project_args(executable, project_name, project_root, compose_file)
        args += ["down", "--remove-orphans=false"]
        return self._run_operation(args, project_root, timeout)

    def _run_operation(
        self, args: list[str], project_root: Path, timeout: float
    ) -> OperationOutcome:
        try:
            result = self._runner(args, cwd=project_root, timeout=timeout, env=self._environment())
        except subprocess.TimeoutExpired as expired:
            return OperationOutcome(
                "timed_out",
                raw_stdout=_captured(expired.stdout),
                raw_stderr=_captured(expired.stderr),
            )
        except OSError:
            return OperationOutcome("unavailable")
        return OperationOutcome(
            "succeeded" if result.returncode == 0 else "failed",
            returncode=result.returncode,
            raw_stdout=result.stdout,
            raw_stderr=result.stderr,
        )

    def list_declared_services(self, *, project_root: Path, compose_file: Path) -> DeclaredServices:
        """Run ``docker compose config --services``; output is names only."""

        executable = self._locate("docker")
        if executable is None:
            return DeclaredServices(error="Docker CLI is not available.")
        args = [
            executable,
            "compose",
            "--project-directory",
            str(project_root),
            "--file",
            str(compose_file),
            "config",
            "--services",
        ]
        try:
            result = self._runner(
                args, cwd=project_root, timeout=CONFIG_TIMEOUT, env=self._environment()
            )
        except (OSError, subprocess.TimeoutExpired):
            return DeclaredServices(error="Compose service listing did not complete.")
        if result.returncode:
            return DeclaredServices(
                error=f"Compose service listing failed with exit code {result.returncode}."
            )
        names = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
        return DeclaredServices(names=names)

    def observe_services(
        self,
        *,
        project_name: str,
        project_root: Path,
        compose_file: Path,
    ) -> ServiceObservations:
        """Run ``docker compose ps --all --format json`` and keep only safe fields."""

        executable = self._locate("docker")
        if executable is None:
            return ServiceObservations(error="Docker CLI is not available.")
        args = self._project_args(executable, project_name, project_root, compose_file)
        args += ["ps", "--all", "--no-trunc", "--format", "json"]
        try:
            result = self._runner(
                args, cwd=project_root, timeout=LIST_TIMEOUT, env=self._environment()
            )
        except (OSError, subprocess.TimeoutExpired):
            return ServiceObservations(error="Service status query did not complete.")
        if result.returncode:
            return ServiceObservations(
                error=f"Service status query failed with exit code {result.returncode}."
            )
        return _parse_service_observations(result.stdout)


def _parse_service_observations(text: str) -> ServiceObservations:
    """Accept a JSON array or newline-delimited objects; both exist across Compose versions."""

    stripped = text.strip()
    if not stripped:
        return ServiceObservations()
    raw_items: list[object]
    try:
        if stripped.startswith("["):
            decoded = json.loads(stripped)
            raw_items = decoded if isinstance(decoded, list) else [decoded]
        else:
            raw_items = [json.loads(line) for line in stripped.splitlines() if line.strip()]
    except json.JSONDecodeError:
        return ServiceObservations(error="Service status output was not valid JSON.")

    items: list[ServiceObservation] = []
    skipped = 0
    for raw in raw_items:
        observation = _parse_observation(raw)
        if observation is None:
            skipped += 1
        else:
            items.append(observation)
    return ServiceObservations(items=tuple(items), skipped=skipped)


def _parse_observation(raw: object) -> ServiceObservation | None:
    if not isinstance(raw, dict):
        return None
    identifier = raw.get("ID")
    name = raw.get("Name")
    service = raw.get("Service")
    state = raw.get("State")
    if not all(isinstance(value, str) and value for value in (identifier, name, service, state)):
        return None
    health = raw.get("Health")
    exit_code = raw.get("ExitCode")
    ports: list[PublishedPort] = []
    publishers = raw.get("Publishers")
    if isinstance(publishers, list):
        for publisher in publishers:
            port = _parse_publisher(publisher)
            if port is not None:
                ports.append(port)
    return ServiceObservation(
        id=str(identifier),
        name=str(name),
        service=str(service),
        state=str(state).lower(),
        health=health.lower() if isinstance(health, str) else "",
        exit_code=exit_code
        if isinstance(exit_code, int) and not isinstance(exit_code, bool)
        else None,
        ports=tuple(sorted(ports, key=lambda item: (item.host_port, item.container_port))),
    )


def _parse_publisher(raw: object) -> PublishedPort | None:
    if not isinstance(raw, dict):
        return None
    published = raw.get("PublishedPort")
    target = raw.get("TargetPort")
    if not isinstance(published, int) or not isinstance(target, int) or published <= 0:
        return None
    url = raw.get("URL")
    protocol = raw.get("Protocol")
    return PublishedPort(
        host_ip=url if isinstance(url, str) else "",
        host_port=published,
        container_port=target,
        protocol=protocol if isinstance(protocol, str) and protocol else "tcp",
    )


_PERMISSION_MARKERS = ("permission denied", "access is denied")


def _daemon_state(result: CommandResult) -> DaemonState:
    """Interpret the version probe; the captured text is only classified, never shown."""

    if result.returncode == 0 and result.stdout.strip():
        return "available"
    lowered = result.stderr.lower()
    if any(marker in lowered for marker in _PERMISSION_MARKERS):
        return "permission_denied"
    return "unavailable"


def _decode_fields(line: str, expected: int) -> list[str] | str:
    """Split a template line and JSON-decode each field; return an error string on failure."""

    fields = line.split(_FIELD_SEPARATOR)
    if len(fields) != expected:
        return f"Expected {expected} fields, got {len(fields)}."
    decoded: list[str] = []
    for field in fields:
        try:
            value = json.loads(field)
        except json.JSONDecodeError:
            return "A field was not valid JSON."
        if not isinstance(value, str):
            return "A field was not a JSON string."
        decoded.append(value)
    return decoded


def _parse_container(line: str) -> ContainerRecord:
    decoded = _decode_fields(line, _CONTAINER_FIELD_COUNT)
    if isinstance(decoded, str):
        return ContainerRecord(id="", name="", state="", parse_error=decoded)
    identifier, name, state, project, working_dir, config_files, service, oneoff = decoded
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
    decoded = _decode_fields(line, 2)
    if isinstance(decoded, str):
        return None
    return NetworkRecord(id=decoded[0], name=decoded[1])


def _parse_volume(line: str) -> str | None:
    decoded = _decode_fields(line, 1)
    return None if isinstance(decoded, str) else decoded[0]


def _captured(stream: str | bytes | None) -> str:
    """Decode partial output attached to a TimeoutExpired, which may be bytes."""

    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream
