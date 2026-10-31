"""Adapter contracts shared by DevDock commands.

An adapter owns every interaction with a local runtime such as Docker Compose.
Command handlers and diagnostics consume the structured results defined here and
never build or run subprocess commands themselves. Lifecycle operations (start,
status, stop, resource identification) are added to the contract in the steps
that implement them so the protocol never advertises unimplemented behavior.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from devdock.cancellation import CancelToken


@dataclass(frozen=True)
class CommandResult:
    """Captured outcome of one bounded subprocess invocation.

    ``stdout`` and ``stderr`` may contain interpolated configuration or
    environment values. Adapters must not copy them into findings or summaries;
    callers that need raw output must opt in explicitly.
    """

    returncode: int
    stdout: str
    stderr: str


class CommandCancelled(Exception):
    """Raised when a command was abandoned because its cancel token was set.

    Carries whatever the child had already written. That output is as
    untrusted as any other, and is named ``raw_`` for the same reason
    ``OperationOutcome``'s fields are.
    """

    def __init__(self, raw_stdout: str = "", raw_stderr: str = "") -> None:
        super().__init__("the command was cancelled")
        self.raw_stdout = raw_stdout
        self.raw_stderr = raw_stderr


class CommandRunner(Protocol):
    """Run an argument array with a timeout and return its captured result.

    Implementations must never pass the command through a shell. They raise
    ``OSError`` when the executable cannot be started,
    ``subprocess.TimeoutExpired`` when ``timeout`` elapses, and
    ``CommandCancelled`` when ``cancel`` is set while the command is running.
    They must not leave a running child behind on any of those paths.
    """

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float,
        env: Mapping[str, str] | None = None,
        cancel: CancelToken | None = None,
    ) -> CommandResult: ...


# The labels Compose writes on everything it creates. They are contract, not
# implementation: ownership rules are expressed in them and the adapter's
# queries read them back, so they belong where both can see them without the
# pure-policy side importing the module that runs `docker`.
PROJECT_LABEL = "com.docker.compose.project"
WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"
SERVICE_LABEL = "com.docker.compose.service"
ONEOFF_LABEL = "com.docker.compose.oneoff"

# Compose-safe project names, which are also inert inside a --filter
# expression. One rule, stated once: ownership derives names against it and
# the adapter re-checks it before every invocation.
SAFE_PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def inspect_command(project_name: str) -> str:
    """The `docker ps` a user can run to see what DevDock is talking about.

    Here rather than in the modules that print it: building a Docker command
    line is adapter knowledge, and two separate callers were assembling the
    same one by hand.
    """

    return f"docker ps -a --filter label={PROJECT_LABEL}={project_name}"


ComposeCliState = Literal["missing", "unresponsive", "failed", "available"]
DaemonState = Literal["not_checked", "unavailable", "permission_denied", "available"]
ConfigOutcome = Literal["valid", "invalid", "unavailable"]


@dataclass(frozen=True)
class ComposeCapabilities:
    """What the local Docker CLI, Compose plugin, and daemon can currently do.

    Captured command output is intentionally absent so these values are safe to
    render verbatim.
    """

    executable: str | None
    compose_cli: ComposeCliState
    compose_cli_returncode: int | None = None
    daemon: DaemonState = "not_checked"

    @property
    def compose_available(self) -> bool:
        return self.compose_cli == "available"

    @property
    def daemon_available(self) -> bool:
        return self.daemon == "available"


@dataclass(frozen=True)
class ConfigValidation:
    """Result of asking the runtime to parse a configuration file."""

    path: Path
    outcome: ConfigOutcome
    returncode: int | None = None

    @property
    def valid(self) -> bool:
        return self.outcome == "valid"


@dataclass(frozen=True)
class ContainerRecord:
    """Identity and ownership labels of one container; never its environment.

    ``parse_error`` is set when the runtime's output for this container could
    not be parsed. Ownership rules treat such records as conflicts.
    """

    id: str
    name: str
    state: str
    project: str = ""
    working_dir: str = ""
    config_files: str = ""
    service: str = ""
    oneoff: str = ""
    parse_error: str | None = None


@dataclass(frozen=True)
class NetworkRecord:
    """A network carrying the project label."""

    id: str
    name: str


@dataclass(frozen=True)
class ResourceInventory:
    """Everything found under one project name, plus any query that failed.

    ``errors`` are safe summaries (which query, exit status); never raw output.
    """

    project_name: str
    containers: tuple[ContainerRecord, ...] = ()
    name_collisions: tuple[ContainerRecord, ...] = ()
    networks: tuple[NetworkRecord, ...] = ()
    volumes: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


OperationStatus = Literal["succeeded", "failed", "timed_out", "unavailable", "cancelled"]


@dataclass(frozen=True)
class OperationOutcome:
    """Result of a mutating operation (start or stop), including raw captured output.

    Unlike the inspect-side result types, this one deliberately carries the
    runtime's raw stdout and stderr, because a failed operation is not
    actionable without them. The fields are named ``raw_`` as a warning: they
    may contain interpolated configuration values and must pass through
    ``devdock.redaction`` before being displayed.
    """

    status: OperationStatus
    returncode: int | None = None
    raw_stdout: str = ""
    raw_stderr: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


@dataclass(frozen=True)
class PublishedPort:
    """One host-published port of a container."""

    host_ip: str
    host_port: int
    container_port: int
    protocol: str

    def render(self) -> str:
        host = self.host_ip or "0.0.0.0"
        return f"{host}:{self.host_port}->{self.container_port}/{self.protocol}"


@dataclass(frozen=True)
class ServiceObservation:
    """Runtime-reported state of one service container.

    Only these fields are retained from the runtime's report. Image, command,
    labels, and anything else that could carry configuration values are
    discarded at parse time.
    """

    id: str
    name: str
    service: str
    state: str
    health: str
    exit_code: int | None
    ports: tuple[PublishedPort, ...] = ()


@dataclass(frozen=True)
class ServiceObservations:
    """All service containers the runtime reported for a project, or why not."""

    items: tuple[ServiceObservation, ...] = ()
    error: str | None = None
    skipped: int = 0


@dataclass(frozen=True)
class DeclaredServices:
    """Service names declared by the project's Compose configuration, or why not."""

    names: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class DeclaredDependency:
    """One ``depends_on`` edge as the Compose configuration states it."""

    service: str
    depends_on: str
    condition: str
    required: bool = True


@dataclass(frozen=True)
class DeclaredDependencies:
    """The startup-ordering facts extracted from a rendered Compose configuration.

    This is everything DevDock keeps from a document that also describes
    images, commands, volumes, and environment. The four fields here are the
    only ones read; the rest is discarded at parse time and never reaches a
    finding, a log line, or an error.

    ``error`` means the configuration could not be read at all, and the caller
    may continue without ordering. ``unsupported`` means it was read but
    contains something DevDock cannot model, which is not safe to continue
    from: ordering derived from a half-understood graph could start a service
    too early.
    """

    services: tuple[str, ...] = ()
    dependencies: tuple[DeclaredDependency, ...] = ()
    healthcheck_declared: frozenset[str] = frozenset()
    healthcheck_disabled: frozenset[str] = frozenset()
    error: str | None = None
    unsupported: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.error is None and not self.unsupported


class RuntimeAdapter(Protocol):
    """Inspect-side operations every runtime adapter provides."""

    def detect_capabilities(self) -> ComposeCapabilities:
        """Report tool, plugin, and daemon availability without mutating anything."""
        ...

    def validate_config(self, config_path: Path, *, project_root: Path) -> ConfigValidation:
        """Ask the runtime to parse ``config_path`` from ``project_root``."""
        ...

    def list_project_resources(self, project_name: str) -> ResourceInventory:
        """List containers, networks, and volumes under ``project_name`` without mutation."""
        ...

    def start(
        self,
        *,
        project_name: str,
        project_root: Path,
        compose_file: Path,
        timeout: float,
        cancel: CancelToken | None = None,
        services: Sequence[str] = (),
        no_deps: bool = False,
        max_parallel: int | None = None,
    ) -> OperationOutcome:
        """Start the project's services detached, without waiting for readiness.

        ``services`` limits the start to those names; empty means the whole
        project.
        """
        ...

    def stop(
        self,
        *,
        project_name: str,
        project_root: Path,
        compose_file: Path,
        timeout: float,
        cancel: CancelToken | None = None,
    ) -> OperationOutcome:
        """Stop and remove the project's containers and networks; never volumes or images."""
        ...

    def list_declared_services(self, *, project_root: Path, compose_file: Path) -> DeclaredServices:
        """Return service names from the configuration without rendering its values."""
        ...

    def list_dependencies(self, *, project_root: Path, compose_file: Path) -> DeclaredDependencies:
        """Return startup-ordering facts only: names, ``depends_on``, health declarations."""
        ...

    def observe_services(
        self,
        *,
        project_name: str,
        project_root: Path,
        compose_file: Path,
    ) -> ServiceObservations:
        """Report state, health, exit code, and published ports per service container."""
        ...
