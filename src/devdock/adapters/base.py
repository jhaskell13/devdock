"""Adapter contracts shared by DevDock commands.

An adapter owns every interaction with a local runtime such as Docker Compose.
Command handlers and diagnostics consume the structured results defined here and
never build or run subprocess commands themselves. Lifecycle operations (start,
status, stop, resource identification) are added to the contract in the steps
that implement them so the protocol never advertises unimplemented behavior.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


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


class CommandRunner(Protocol):
    """Run an argument array with a timeout and return its captured result.

    Implementations must never pass the command through a shell. They raise
    ``OSError`` when the executable cannot be started and
    ``subprocess.TimeoutExpired`` when ``timeout`` elapses.
    """

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float,
    ) -> CommandResult: ...


ComposeCliState = Literal["missing", "unresponsive", "failed", "available"]
DaemonState = Literal["not_checked", "unavailable", "available"]
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
