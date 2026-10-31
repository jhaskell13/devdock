"""Docker Compose adapter built on argument arrays and bounded subprocesses."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from devdock.adapters.base import (
    CommandCancelled,
    CommandResult,
    CommandRunner,
    ComposeCapabilities,
    ConfigValidation,
    ContainerRecord,
    DaemonState,
    DeclaredDependencies,
    DeclaredDependency,
    DeclaredServices,
    NetworkRecord,
    OperationOutcome,
    PublishedPort,
    ResourceInventory,
    ServiceObservation,
    ServiceObservations,
)
from devdock.cancellation import CancelToken

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


# Windows' CREATE_NEW_PROCESS_GROUP. Named here because the constant does not
# exist in `subprocess` on other platforms, so it cannot be referenced directly
# in code that must import everywhere.
WINDOWS_NEW_PROCESS_GROUP = 0x00000200

# How long a child is given to exit after being asked, before it is killed.
TERMINATION_GRACE = 5.0
# How often a running child is checked for cancellation. Ten wake-ups a second
# costs nothing next to the processes being waited on, and bounds how long a
# Ctrl+C appears to do nothing.
CANCEL_POLL_INTERVAL = 0.1
# Output beyond this is dropped. The whole stream is written to a temporary
# file rather than a pipe, so this bounds what DevDock holds in memory rather
# than what the child may produce.
MAX_CAPTURED_BYTES = 1_000_000


@dataclass(frozen=True)
class ProcessIsolation:
    """How one platform asks for a child in a process group of its own.

    Both fields are always passed to ``Popen``; exactly one of them does
    anything on any given platform, and the other is inert there. Carried as a
    value rather than applied directly so each platform's choice can be
    asserted on every platform, which matters more here than usual: Windows
    accepts ``start_new_session`` and *ignores* it rather than refusing it, so
    choosing the wrong field there would isolate nothing and say nothing.
    """

    creationflags: int = 0
    start_new_session: bool = False


def process_isolation(platform_name: str = os.name) -> ProcessIsolation:
    """Isolation settings for ``platform_name``, defaulting to this machine.

    Without isolation, a Ctrl+C in the terminal goes to every process in the
    foreground group, so the terminal -- not DevDock -- interrupts ``docker``
    halfway through whatever it was doing, and DevDock learns about it
    afterwards. With it, the signal arrives only here and DevDock decides what
    the child does.
    """

    if platform_name == "nt":
        return ProcessIsolation(
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", WINDOWS_NEW_PROCESS_GROUP)
        )
    return ProcessIsolation(start_new_session=True)


def _stop_child(process: subprocess.Popen[bytes], grace: float = TERMINATION_GRACE) -> None:
    """End a child and everything it started, politely first where that exists.

    On POSIX the whole process group is signalled, so a child that spawned its
    own helpers does not leave them behind. The group id is the child's pid,
    because ``start_new_session`` made it the leader of a new one; reading it
    back with ``getpgid`` would be a second chance to fail. The pid cannot have
    been reused, because nothing has reaped the child yet.

    Windows has no graceful equivalent: ``terminate`` is ``TerminateProcess``,
    which is immediate, and it reaches only the child itself. A grandchild
    there is not DevDock's to clean up. It also has no ``SIGKILL``, which is
    why the two stages are named rather than carried as signal numbers --
    naming the numbers up front would look at ``signal.SIGKILL`` on a platform
    that does not define it, and fail there for every command.
    """

    if process.poll() is not None:
        return
    for stage in ("ask", "force"):
        try:
            if os.name == "nt":
                process.terminate() if stage == "ask" else process.kill()
            else:
                os.killpg(process.pid, signal.SIGTERM if stage == "ask" else signal.SIGKILL)
        except (OSError, ValueError):
            # Already gone, or no longer ours to signal. Either way there is
            # nothing to clean up and nothing useful to report.
            return
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def _read_capped(handle: IO[bytes]) -> str:
    """Read back captured output, bounded, decoded, with newlines normalized.

    Newline translation is done here rather than by opening the child's stream
    in text mode so that a Windows child's output is byte-for-byte the same
    string a POSIX child's would be, and every downstream rule -- failure
    classification, redaction, line counting -- behaves identically.
    """

    handle.seek(0)
    data = handle.read(MAX_CAPTURED_BYTES + 1)
    truncated = len(data) > MAX_CAPTURED_BYTES
    text = data[:MAX_CAPTURED_BYTES].decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if truncated:
        text += f"\n[DevDock stopped reading after {MAX_CAPTURED_BYTES} bytes.]"
    return text


def run_command(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cancel: CancelToken | None = None,
) -> CommandResult:
    """Default runner: no shell, bounded captured output, enforced timeout.

    The child runs in its own process group and is waited for in short steps
    so a cancellation is noticed promptly. Every exit from this function --
    returning, timing out, cancelling, or any exception including
    ``KeyboardInterrupt`` and ``SystemExit`` -- leaves no running child behind.
    """

    arguments = list(args)
    isolation = process_isolation()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        # Temporary files rather than pipes: the child can write as much as it
        # likes without filling a pipe buffer and blocking, partial output
        # survives a timeout or a cancellation, and how much DevDock reads
        # back is this process's decision rather than the child's.
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            env=None if env is None else dict(env),
            creationflags=isolation.creationflags,
            start_new_session=isolation.start_new_session,
        )
        try:
            started = time.monotonic()
            while True:
                try:
                    process.wait(timeout=CANCEL_POLL_INTERVAL)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if cancel is not None and cancel.cancelled:
                    _stop_child(process)
                    raise CommandCancelled(_read_capped(out), _read_capped(err))
                if time.monotonic() - started >= timeout:
                    _stop_child(process)
                    raise subprocess.TimeoutExpired(
                        arguments, timeout, output=_read_capped(out), stderr=_read_capped(err)
                    )
            return CommandResult(process.returncode, _read_capped(out), _read_capped(err))
        finally:
            # Covers every path the loop above does not: an interrupt
            # delivered between two waits, a SystemExit raised by a signal
            # handler, or anything unforeseen. A child outliving the command
            # that started it is the one outcome with no way back.
            _stop_child(process)


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
        cancel: CancelToken | None = None,
        services: Sequence[str] = (),
        no_deps: bool = False,
        max_parallel: int | None = None,
    ) -> OperationOutcome:
        """Run ``docker compose up --detach``, optionally for named services only.

        DevDock supplies the project name, project directory, Compose file,
        and these options itself; no user-provided Compose options are
        forwarded. The command returns once containers are created and
        started, which is not the same as being ready: waiting is the wait
        engine's job, not a flag passed to Compose.

        ``services`` scopes the start to a batch whose dependencies the caller
        has already satisfied, which is what makes a staged start possible.
        ``no_deps`` then stops Compose from pulling in and re-ordering
        dependencies the caller is already managing; it is only safe when the
        caller's graph came from Compose in the first place, so the decision
        is the caller's rather than implied by ``services``.
        """

        executable = self._locate("docker")
        if executable is None:
            return OperationOutcome("unavailable")
        names = [str(name) for name in services]
        for name in names:
            if not COMPOSE_SERVICE_NAME.fullmatch(name):
                raise ValueError("service name is not safe to pass as an argument")
        args = self._project_args(
            executable, project_name, project_root, compose_file, max_parallel=max_parallel
        )
        args += ["up", "--detach", "--remove-orphans=false"]
        if no_deps:
            args.append("--no-deps")
        if names:
            # Everything after "--" is a service name, never an option. Compose
            # is Cobra-based and without it a service legitimately named
            # "-weird" is read as flags: verified, it reports
            # "unknown shorthand flag: 'e'". The names are validated above as
            # well; this is the boundary where it actually matters.
            args.append("--")
            args.extend(names)
        return self._run_operation(args, project_root, timeout, cancel)

    def _project_args(
        self,
        executable: str,
        project_name: str,
        project_root: Path,
        compose_file: Path,
        *,
        max_parallel: int | None = None,
    ) -> list[str]:
        """Common ``docker compose`` prefix; rejects names that are unsafe in filters.

        ``--parallel`` is one of Compose's own global options and has to
        precede the subcommand, which is why it belongs here rather than
        beside the ``up`` arguments.
        """

        if not SAFE_PROJECT_NAME.fullmatch(project_name):
            raise ValueError(f"project name is not filter-safe: {project_name!r}")
        parallel = [] if max_parallel is None else ["--parallel", str(int(max_parallel))]
        return [
            executable,
            "compose",
            *parallel,
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
        cancel: CancelToken | None = None,
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
        return self._run_operation(args, project_root, timeout, cancel)

    def _run_operation(
        self,
        args: list[str],
        project_root: Path,
        timeout: float,
        cancel: CancelToken | None = None,
    ) -> OperationOutcome:
        try:
            result = self._runner(
                args, cwd=project_root, timeout=timeout, env=self._environment(), cancel=cancel
            )
        except CommandCancelled as cancelled:
            return OperationOutcome(
                "cancelled",
                raw_stdout=cancelled.raw_stdout,
                raw_stderr=cancelled.raw_stderr,
            )
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

    def list_dependencies(self, *, project_root: Path, compose_file: Path) -> DeclaredDependencies:
        """Read startup-ordering facts from the rendered Compose configuration.

        This is the only DevDock command that asks Compose for the whole
        project document, because ``depends_on`` is the one fact no narrower
        query exposes, and because Compose is the only thing that can resolve
        ``extends``, ``include``, overrides, and profiles correctly. Parsing
        the YAML here instead would produce a confidently wrong graph.

        ``--no-interpolate`` is not an optimization. Without it, Compose
        renders ``${VAR}`` substitutions *and* the contents of every
        ``env_file`` into the ``environment`` of each service, so the document
        would carry the project's secrets. With it, variables stay as written
        and ``env_file`` is reported as a path only. Both were verified against
        a real Compose. What survives parsing is only the four fields in
        ``DeclaredDependencies``; the document is not retained, and no part of
        it appears in any error this method returns.
        """

        executable = self._locate("docker")
        if executable is None:
            return DeclaredDependencies(error="Docker CLI is not available.")
        args = [
            executable,
            "compose",
            "--project-directory",
            str(project_root),
            "--file",
            str(compose_file),
            "config",
            "--format",
            "json",
            "--no-interpolate",
        ]
        try:
            result = self._runner(
                args, cwd=project_root, timeout=CONFIG_TIMEOUT, env=self._environment()
            )
        except (OSError, subprocess.TimeoutExpired):
            return DeclaredDependencies(error="Compose dependency listing did not complete.")
        if result.returncode:
            return DeclaredDependencies(
                error=f"Compose dependency listing failed with exit code {result.returncode}."
            )
        return _parse_dependencies(result.stdout)

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


# The rendered configuration is read into memory before anything is extracted
# from it, so the size it may reach is bounded here rather than trusted. A
# project large enough to exceed this is better served by a clear refusal than
# by an unbounded parse of a document DevDock wants four fields from.
MAX_CONFIG_CHARACTERS = 4_000_000

# Compose's own service-name shape. Its schema already enforces this, so a name
# that fails here means the output did not come from a Compose that DevDock
# understands. Names are not quoted when rejected: an unexpected name is
# exactly the value least safe to echo. A name legal here can still begin with
# a character that argv would read as an option, which is re-checked where
# names become command arguments rather than assumed here.
COMPOSE_SERVICE_NAME = re.compile(r"^[a-zA-Z0-9._-]{1,63}$")

# What Compose's schema accepts today. A condition outside this set means a
# newer Compose, not a malformed file, and DevDock must not guess at ordering
# it does not understand.
COMPOSE_CONDITIONS = frozenset(
    {"service_started", "service_healthy", "service_completed_successfully"}
)
# A condition is runtime-supplied text. It is echoed only when it looks like a
# Compose condition identifier, so an unexpected value is described rather than
# repeated.
_QUOTABLE_CONDITION = re.compile(r"^[a-z_]{1,40}$")


def _parse_dependencies(text: str) -> DeclaredDependencies:
    """Keep the ordering facts from a rendered configuration and discard the rest.

    Nothing read here is returned except service names, dependency edges, and
    which services declare or disable a health check. Errors name the failure,
    never the content that caused it.
    """

    if len(text) > MAX_CONFIG_CHARACTERS:
        return DeclaredDependencies(
            error="Compose configuration was too large for DevDock to parse safely."
        )
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return DeclaredDependencies(error="Compose configuration output was not valid JSON.")
    except RecursionError:
        # Not a JSONDecodeError, so it would otherwise escape as a traceback.
        # Compose echoes `x-` extension fields verbatim and they accept any
        # structure, so nesting deep enough to exhaust the parser's stack can
        # come from a project file.
        return DeclaredDependencies(
            error="Compose configuration output was nested too deeply to parse."
        )
    if not isinstance(document, dict):
        return DeclaredDependencies(error="Compose configuration output was not an object.")
    raw_services = document.get("services")
    if raw_services is None:
        return DeclaredDependencies()
    if not isinstance(raw_services, dict):
        return DeclaredDependencies(
            error="Compose configuration output did not describe services as an object."
        )

    names: list[str] = []
    rejected = 0
    for name in raw_services:
        if isinstance(name, str) and COMPOSE_SERVICE_NAME.fullmatch(name):
            names.append(name)
        else:
            rejected += 1
    if rejected:
        return DeclaredDependencies(
            error=f"Compose configuration named {rejected} service(s) in a form DevDock does "
            "not recognize."
        )

    dependencies: list[DeclaredDependency] = []
    declared_health: set[str] = set()
    disabled_health: set[str] = set()
    unsupported: set[str] = set()
    for name in names:
        definition = raw_services[name]
        if not isinstance(definition, dict):
            return DeclaredDependencies(
                error="Compose configuration described a service in a form DevDock does not "
                "recognize."
            )
        health = definition.get("healthcheck")
        if isinstance(health, dict):
            if health.get("disable") is True:
                disabled_health.add(name)
            else:
                declared_health.add(name)
        edges, problems = _parse_depends_on(name, definition.get("depends_on"))
        if problems is not None:
            return DeclaredDependencies(error=problems)
        dependencies.extend(edges)
        unsupported.update(
            _unsupported_condition(edge.condition)
            for edge in edges
            if edge.condition not in COMPOSE_CONDITIONS
        )

    if unsupported:
        # Nothing else comes back. The edges are unusable regardless, and one
        # of them holds the unrecognized condition verbatim -- the only value
        # in this result that did not come from a vocabulary DevDock fixed in
        # advance. Dropping them keeps it away from a caller that might render
        # it.
        return DeclaredDependencies(unsupported=tuple(sorted(unsupported)))

    return DeclaredDependencies(
        services=tuple(sorted(names)),
        dependencies=tuple(sorted(dependencies, key=lambda edge: (edge.service, edge.depends_on))),
        healthcheck_declared=frozenset(declared_health),
        healthcheck_disabled=frozenset(disabled_health),
    )


def _unsupported_condition(condition: str) -> str:
    named = condition if _QUOTABLE_CONDITION.fullmatch(condition) else "an unprintable value"
    return (
        f"Compose reported the dependency condition {named}, which this version of DevDock "
        "cannot order against."
    )


def _parse_depends_on(service: str, raw: object) -> tuple[list[DeclaredDependency], str | None]:
    """Read one service's dependencies from either Compose form.

    Current Compose normalizes the short list form into the mapping form, but
    the list form is still accepted here so an older Compose does not silently
    produce a graph with no edges.
    """

    if raw is None:
        return [], None
    edges: list[DeclaredDependency] = []
    if isinstance(raw, list):
        for target in raw:
            if not isinstance(target, str) or not COMPOSE_SERVICE_NAME.fullmatch(target):
                return [], _unrecognized_dependency(service)
            edges.append(DeclaredDependency(service, target, "service_started"))
        return edges, None
    if not isinstance(raw, dict):
        return [], _unrecognized_dependency(service)
    for target, settings in raw.items():
        if not isinstance(target, str) or not COMPOSE_SERVICE_NAME.fullmatch(target):
            return [], _unrecognized_dependency(service)
        condition = "service_started"
        required = True
        if isinstance(settings, dict):
            raw_condition = settings.get("condition")
            if isinstance(raw_condition, str) and raw_condition:
                condition = raw_condition
            required = settings.get("required") is not False
        elif settings is not None:
            return [], _unrecognized_dependency(service)
        edges.append(DeclaredDependency(service, target, condition, required))
    return edges, None


def _unrecognized_dependency(service: str) -> str:
    return (
        f"Compose described a dependency of service {service} in a form DevDock does not recognize."
    )


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
    except RecursionError:  # same hazard as the configuration parser; not a decode error
        return ServiceObservations(error="Service status output was nested too deeply to parse.")

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
