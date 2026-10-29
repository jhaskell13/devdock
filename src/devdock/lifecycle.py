"""Policy for lifecycle commands: what must be true before the runtime is asked to act.

Command handlers render these results and choose an exit code; they contain no
Docker knowledge. Adapters execute; they contain no policy. Every gate here
runs before any mutation, and the ownership preview is printed whether or not
the action proceeds.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from devdock.adapters import ComposeAdapter, ContainerRecord, NetworkRecord, RuntimeAdapter
from devdock.adapters.compose import PROJECT_LABEL, START_TIMEOUT, STOP_TIMEOUT
from devdock.config import ProjectProfile
from devdock.diagnostics import Finding, check_profile
from devdock.failures import PERMISSION_DENIED_REMEDY, FailureExplanation, classify_failure
from devdock.ownership import (
    OwnershipAssessment,
    OwnershipScope,
    assess_ownership,
    ownership_scope,
    resolves_inside,
)
from devdock.redaction import redact, summarize
from devdock.status import ProjectStatus, build_service_statuses


@dataclass(frozen=True)
class LifecycleResult:
    """Outcome of one lifecycle command, with display lines already redacted."""

    code: str
    ok: bool
    summary: str
    lines: tuple[str, ...] = ()
    remedy: str | None = None


# Port probes are advisory for a start. A port that a diagnostic sees as taken
# is frequently taken by this project's own running services, so treating it as
# a blocking failure would make a second `devdock up` refuse. The authoritative
# test is Compose's own bind, which reports UP_PORT_CONFLICT when it fails.
PREFLIGHT_ADVISORY_CODES = frozenset({"PORT_UNAVAILABLE"})
ENDPOINT_HOLDING_STATES = frozenset({"running", "paused", "restarting"})


def _refusal(code: str, summary: str, lines: tuple[str, ...], remedy: str) -> LifecycleResult:
    return LifecycleResult(code, False, summary, lines, remedy)


def _gates(
    scope: OwnershipScope, runtime: RuntimeAdapter, prefix: str, lead: str
) -> Path | LifecycleResult:
    """Checks every lifecycle command shares, in order: Compose file, then runtime.

    Returns the usable Compose file path, or the refusal that stops the command.
    One implementation keeps the three commands from drifting apart; the
    permission-denied gate once had to be added in three places.
    """

    if scope.compose_file is None:
        return _refusal(
            f"{prefix}_COMPOSE_FILE_MISSING",
            f"{lead}: no Compose file is configured or discoverable.",
            (f"Project root: {scope.root}",),
            "Set compose_file in devdock.yaml or add a conventional Compose filename.",
        )
    if not resolves_inside(scope.compose_file, scope.root):
        return _refusal(
            f"{prefix}_COMPOSE_FILE_OUTSIDE_PROJECT",
            f"{lead}: the Compose file does not resolve inside the project.",
            (f"Compose file: {scope.compose_file}",),
            "Remove the escaping symlink or choose a project-contained Compose file.",
        )
    if not scope.compose_file.is_file():
        return _refusal(
            f"{prefix}_COMPOSE_FILE_MISSING",
            f"{lead}: the configured Compose file does not exist.",
            (f"Expected path: {scope.compose_file}",),
            "Restore the file or update compose_file in devdock.yaml. Compose needs it to "
            "identify the project's resources.",
        )

    capabilities = runtime.detect_capabilities()
    if not capabilities.compose_available:
        return _refusal(
            f"{prefix}_RUNTIME_UNAVAILABLE",
            f"{lead}: the Docker Compose CLI is not available.",
            (),
            "Install Docker with the Compose plugin, then retry.",
        )
    if capabilities.daemon == "permission_denied":
        return _refusal(
            f"{prefix}_PERMISSION_DENIED",
            f"{lead}: this user may not connect to the Docker daemon.",
            (),
            PERMISSION_DENIED_REMEDY,
        )
    if not capabilities.daemon_available:
        return _refusal(
            f"{prefix}_DAEMON_UNAVAILABLE",
            f"{lead}: the Docker daemon did not respond.",
            (),
            "Start Docker Desktop or the Docker Engine service and retry.",
        )
    return scope.compose_file


def _inspect_hint(scope: OwnershipScope) -> str:
    return f"Inspect with: docker ps -a --filter label={PROJECT_LABEL}={scope.project_name}"


def start_project(
    profile: ProjectProfile,
    root: Path,
    *,
    adapter: RuntimeAdapter | None = None,
    environ: Mapping[str, str] | None = None,
    timeout: float = START_TIMEOUT,
    show_output: bool = False,
) -> LifecycleResult:
    """Validate, prove ownership, then start the project's services detached."""

    runtime = ComposeAdapter() if adapter is None else adapter
    env = os.environ if environ is None else environ

    findings: list[Finding] = check_profile(profile, root, environ=env, adapter=runtime)
    advisories = tuple(
        f"[ADVISORY] {item.code}: {item.summary}"
        for item in findings
        if item.severity == "FAIL" and item.code in PREFLIGHT_ADVISORY_CODES
    )
    if advisories:
        advisories += (
            "Advisory findings did not block this start; a port that looks taken is "
            "usually taken by this project's own services, and Compose's own bind decides.",
        )
    failures = tuple(
        item
        for item in findings
        if item.severity == "FAIL" and item.code not in PREFLIGHT_ADVISORY_CODES
    )
    if failures:
        return _refusal(
            "UP_PREFLIGHT_FAILED",
            "Refusing to start: the project's own checks failed.",
            tuple(f"[{item.severity}] {item.code}: {item.summary}" for item in failures),
            "Resolve the failures above, confirm with 'devdock check', then retry.",
        )

    scope = ownership_scope(profile, root)
    compose_file = _gates(scope, runtime, "UP", "Refusing to start")
    if isinstance(compose_file, LifecycleResult):
        return compose_file

    assessment = assess_ownership(scope, runtime.list_project_resources(scope.project_name))
    preview = assessment.explain() + advisories
    if not assessment.safe_to_act:
        return _refusal(
            "UP_OWNERSHIP_CONFLICT",
            "Refusing to start: resources under this project name are not DevDock's.",
            preview,
            "Resolve the conflicts above, then retry.",
        )

    outcome = runtime.start(
        project_name=scope.project_name,
        project_root=scope.root,
        compose_file=compose_file,
        timeout=timeout,
    )
    captured = _render_output(outcome.raw_stdout, outcome.raw_stderr, env, show_output)

    if outcome.status == "unavailable":
        return _refusal(
            "UP_RUNTIME_UNAVAILABLE",
            "The Docker CLI could not be started.",
            preview,
            "Confirm Docker is installed and on PATH, then retry.",
        )
    if outcome.status == "timed_out":
        return _refusal(
            "UP_TIMED_OUT",
            f"Start did not finish within {timeout:g} seconds and was stopped.",
            preview
            + ("Containers may have been created before the timeout.", _inspect_hint(scope))
            + captured,
            "Run 'devdock up' again with a longer --timeout, or inspect the project with the "
            "docker ps command above.",
        )
    if outcome.status == "failed":
        failure = classify_failure("UP", outcome.raw_stdout, outcome.raw_stderr)
        return _refusal(
            failure.code,
            f"Could not start {scope.project_name}: {failure.summary}",
            preview
            + _failure_lines(failure, outcome.returncode)
            + _render_output(
                outcome.raw_stdout,
                outcome.raw_stderr,
                env,
                show_output,
                recognized=failure.recognized,
            ),
            failure.remedy,
        )
    return LifecycleResult(
        "UP_STARTED",
        True,
        f"Started {scope.project_name}.",
        preview
        + _render_output(outcome.raw_stdout, outcome.raw_stderr, env, show_output, on_success=True)
        + (
            "Services were started but readiness was not verified; a container can be "
            "running without being ready to serve requests.",
        ),
    )


def _render_output(
    stdout: str,
    stderr: str,
    environ: Mapping[str, str],
    show_output: bool,
    *,
    recognized: bool = False,
    on_success: bool = False,
) -> tuple[str, ...]:
    """Return redacted runtime output, scaled to how much the caller still needs.

    A successful operation needs none of it: DevDock has already described what
    changed in its own words. A recognized failure needs none of it either,
    because it carries a precise summary and remedy. In both cases showing the
    text would risk disclosing values DevDock cannot recognize for no
    diagnostic gain, so it waits behind --show-output.
    """

    if show_output:
        lines: list[str] = []
        for label, stream in (("stdout", stdout), ("stderr", stderr)):
            body = redact(stream, environ).splitlines()
            if body:
                lines.append(f"Runtime {label}:")
                lines.extend(f"  {line.rstrip()}" for line in body)
        if lines:
            lines.append(
                "Output above is redacted for values in DevDock's environment; values "
                "read by Compose from the project's .env cannot be recognized."
            )
        return tuple(lines)
    if on_success:
        return ()
    if recognized:
        return ("Runtime output is hidden; rerun with --show-output to see it.",)
    tail = summarize(stderr or stdout, environ)
    return tuple(f"  {line}" for line in tail)


def project_status(
    profile: ProjectProfile,
    root: Path,
    *,
    adapter: RuntimeAdapter | None = None,
) -> LifecycleResult:
    """Report per-service state, health, readiness, and ports without mutating anything.

    Unlike ``start_project`` this does not run the profile's diagnostics first:
    a status report is most useful precisely when something is wrong, so only
    the inputs it needs (a Compose file and a responsive runtime) are required.
    Ownership conflicts are reported, not treated as a refusal.
    """

    runtime = ComposeAdapter() if adapter is None else adapter
    scope = ownership_scope(profile, root)
    compose_file = _gates(scope, runtime, "STATUS", "Cannot report status")
    if isinstance(compose_file, LifecycleResult):
        return compose_file

    assessment = assess_ownership(scope, runtime.list_project_resources(scope.project_name))
    declared = runtime.list_declared_services(project_root=scope.root, compose_file=compose_file)
    observed = runtime.observe_services(
        project_name=scope.project_name,
        project_root=scope.root,
        compose_file=compose_file,
    )
    services, notes = build_service_statuses(declared.names, assessment.managed, observed.items)
    errors = list(assessment.errors)
    if declared.error:
        errors.append(declared.error)
    if observed.error:
        errors.append(observed.error)
    extra_notes = list(notes)
    if observed.skipped:
        extra_notes.append(
            f"{observed.skipped} runtime status entr{'y' if observed.skipped == 1 else 'ies'} "
            "could not be parsed and were ignored."
        )
    report = ProjectStatus(
        scope=scope,
        services=services,
        conflicts=assessment.conflicts,
        errors=tuple(errors),
        notes=tuple(extra_notes),
    )
    if not report.complete:
        return LifecycleResult(
            "STATUS_INCOMPLETE",
            False,
            "Status report is incomplete; some runtime queries failed.",
            report.explain(),
            "Retry; if it persists, run the docker commands above by hand to see the error.",
        )
    return LifecycleResult(
        "STATUS_REPORTED", True, f"Status for {scope.project_name}.", report.explain()
    )


def _failure_lines(failure: FailureExplanation, returncode: int | None) -> tuple[str, ...]:
    """One evidence line naming the exit status and whether DevDock recognized it."""

    recognition = "recognized" if failure.recognized else "not recognized by DevDock"
    return (f"Runtime exit code: {returncode} ({recognition}).",)


@dataclass(frozen=True)
class StopPlan:
    """Preview of a stop: exactly what will change and what will not."""

    scope: OwnershipScope
    assessment: OwnershipAssessment
    affected: tuple[ContainerRecord, ...]
    remaining: tuple[ContainerRecord, ...]
    networks: tuple[NetworkRecord, ...]
    volumes: tuple[str, ...]

    @property
    def remaining_in_use(self) -> tuple[ContainerRecord, ...]:
        """Leftover containers that still hold a network endpoint.

        Docker releases endpoints when a container stops, so only running,
        paused, or restarting leftovers keep Compose from removing a network.
        Verified against a real daemon in both states.
        """

        return tuple(
            item for item in self.remaining if item.state.lower() in ENDPOINT_HOLDING_STATES
        )

    @property
    def networks_removable(self) -> tuple[NetworkRecord, ...]:
        """Networks Compose can remove: none while a running leftover uses them."""

        return () if self.remaining_in_use else self.networks

    @property
    def nothing_to_do(self) -> bool:
        return not self.affected and not self.networks_removable

    def explain(self) -> tuple[str, ...]:
        lines = list(self.scope.describe(boundary=False))
        if self.affected:
            lines.append(f"Will stop and remove {len(self.affected)} container(s):")
            lines.extend(f"  {item.name} [{item.service}] {item.state}" for item in self.affected)
        else:
            lines.append("Will stop and remove containers: none are running or stopped.")
        if self.networks_removable:
            lines.append(f"Will remove {len(self.networks_removable)} project network(s):")
            lines.extend(f"  {item.name}" for item in self.networks_removable)
        if self.remaining:
            lines.append(
                f"Will leave {len(self.remaining)} DevDock container(s) whose service is no "
                "longer declared in the Compose file:"
            )
            lines.extend(f"  {item.name} [{item.service}]" for item in self.remaining)
            if self.remaining_in_use and self.networks:
                lines.append(
                    f"Will leave {len(self.networks)} project network(s) in place; "
                    f"{len(self.remaining_in_use)} running undeclared container(s) still use them:"
                )
                lines.extend(f"  {item.name}" for item in self.networks)
            lines.append(
                "To remove undeclared containers, restore their service to the Compose file "
                "and run 'devdock down' again, or remove them with 'docker rm'."
            )
        if self.volumes:
            lines.append(
                f"Will preserve {len(self.volumes)} volume(s); DevDock never removes "
                "volumes in this version:"
            )
            lines.extend(f"  {name}" for name in self.volumes)
        else:
            lines.append("Volumes: none under this project name.")
        return tuple(lines)


def plan_stop(
    profile: ProjectProfile,
    root: Path,
    *,
    adapter: RuntimeAdapter | None = None,
) -> StopPlan | LifecycleResult:
    """Build the stop preview, or a refusal if the project cannot be acted on."""

    runtime = ComposeAdapter() if adapter is None else adapter
    scope = ownership_scope(profile, root)
    compose_file = _gates(scope, runtime, "DOWN", "Refusing to stop")
    if isinstance(compose_file, LifecycleResult):
        return compose_file

    assessment = assess_ownership(scope, runtime.list_project_resources(scope.project_name))
    if not assessment.safe_to_act:
        return _refusal(
            "DOWN_OWNERSHIP_CONFLICT",
            "Refusing to stop: resources under this project name are not DevDock's.",
            assessment.explain(),
            "Resolve the conflicts above, then retry.",
        )
    declared = runtime.list_declared_services(project_root=scope.root, compose_file=compose_file)
    if declared.error:
        return _refusal(
            "DOWN_CONFIG_UNREADABLE",
            "Refusing to stop: the Compose file's services could not be listed.",
            (declared.error,),
            "Run 'docker compose config' locally to see the error, then correct the file.",
        )
    declared_names = set(declared.names)
    affected = tuple(item for item in assessment.managed if item.service in declared_names)
    remaining = tuple(item for item in assessment.managed if item.service not in declared_names)
    return StopPlan(
        scope=scope,
        assessment=assessment,
        affected=affected,
        remaining=remaining,
        networks=assessment.networks,
        volumes=assessment.volumes,
    )


def execute_stop(
    plan: StopPlan,
    *,
    adapter: RuntimeAdapter | None = None,
    environ: Mapping[str, str] | None = None,
    timeout: float = STOP_TIMEOUT,
    show_output: bool = False,
) -> LifecycleResult:
    """Carry out an approved stop plan and verify what remains afterwards."""

    runtime = ComposeAdapter() if adapter is None else adapter
    env = os.environ if environ is None else environ
    scope = plan.scope
    compose_file = scope.compose_file
    if compose_file is None:  # plan_stop never produces this; guard the public API
        raise ValueError("a stop plan requires a Compose file")
    if plan.nothing_to_do:
        return LifecycleResult(
            "DOWN_NOTHING_TO_STOP",
            True,
            f"Nothing to stop for {scope.project_name}; volumes, if any, are untouched.",
        )

    current = assess_ownership(scope, runtime.list_project_resources(scope.project_name))
    planned_ids = {item.id for item in plan.assessment.managed}
    current_ids = {item.id for item in current.managed}
    if not current.safe_to_act or current_ids != planned_ids:
        return _refusal(
            "DOWN_PLAN_STALE",
            "Refusing to stop: the project's resources changed after the plan was shown.",
            current.explain(),
            "Run 'devdock down' again to see and confirm the current plan.",
        )

    outcome = runtime.stop(
        project_name=scope.project_name,
        project_root=scope.root,
        compose_file=compose_file,
        timeout=timeout,
    )
    captured = _render_output(outcome.raw_stdout, outcome.raw_stderr, env, show_output)
    if outcome.status == "unavailable":
        return _refusal(
            "DOWN_RUNTIME_UNAVAILABLE",
            "The Docker CLI could not be started.",
            (),
            "Confirm Docker is installed and on PATH, then retry.",
        )
    if outcome.status == "timed_out":
        return _refusal(
            "DOWN_TIMED_OUT",
            f"Stop did not finish within {timeout:g} seconds and was interrupted.",
            ("Some containers may already be stopped or removed.",) + captured,
            "Run 'devdock status' to see what remains, then run 'devdock down' again.",
        )
    if outcome.status == "failed":
        failure = classify_failure("DOWN", outcome.raw_stdout, outcome.raw_stderr)
        return _refusal(
            failure.code,
            f"Could not stop {scope.project_name}: {failure.summary}",
            _failure_lines(failure, outcome.returncode)
            + _render_output(
                outcome.raw_stdout,
                outcome.raw_stderr,
                env,
                show_output,
                recognized=failure.recognized,
            ),
            failure.remedy,
        )

    after = assess_ownership(scope, runtime.list_project_resources(scope.project_name))
    lines: list[str] = list(
        _render_output(outcome.raw_stdout, outcome.raw_stderr, env, show_output, on_success=True)
    )
    leftover = tuple(item for item in after.managed if item.id in {a.id for a in plan.affected})
    if leftover:
        lines.append(f"Warning: {len(leftover)} container(s) still exist after down:")
        lines.extend(f"  {item.name} {item.state}" for item in leftover)
    unexpected_networks = () if plan.remaining_in_use else after.networks
    if unexpected_networks:
        lines.append(f"Warning: {len(unexpected_networks)} project network(s) still exist.")
    if plan.remaining:
        lines.append(
            f"Left in place as planned: {len(plan.remaining)} container(s) from undeclared "
            "services."
        )
    if after.volumes:
        lines.append(f"Preserved {len(after.volumes)} volume(s): " + ", ".join(after.volumes))
    if after.errors:
        lines.append(
            "Could not verify the result; the post-stop inventory was incomplete: "
            + "; ".join(after.errors)
        )
    verified = not leftover and not unexpected_networks and not after.errors
    code = "DOWN_STOPPED" if verified else "DOWN_PARTIAL"
    return LifecycleResult(
        code,
        verified,
        f"Stopped {scope.project_name}."
        if verified
        else f"Stopped {scope.project_name} but could not confirm everything was removed.",
        tuple(lines),
        None if verified else "Run 'devdock status' and retry 'devdock down'.",
    )
