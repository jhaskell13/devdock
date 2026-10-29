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

from devdock.adapters import ComposeAdapter, RuntimeAdapter
from devdock.adapters.compose import START_TIMEOUT
from devdock.config import ProjectProfile
from devdock.diagnostics import Finding, check_profile
from devdock.ownership import assess_ownership, ownership_scope, resolves_inside
from devdock.redaction import redact, summarize


@dataclass(frozen=True)
class LifecycleResult:
    """Outcome of one lifecycle command, with display lines already redacted."""

    code: str
    ok: bool
    summary: str
    lines: tuple[str, ...] = ()
    remedy: str | None = None


def _refusal(code: str, summary: str, lines: tuple[str, ...], remedy: str) -> LifecycleResult:
    return LifecycleResult(code, False, summary, lines, remedy)


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
    failures = tuple(item for item in findings if item.severity == "FAIL")
    if failures:
        return _refusal(
            "UP_PREFLIGHT_FAILED",
            "Refusing to start: the project's own checks failed.",
            tuple(f"[{item.severity}] {item.code}: {item.summary}" for item in failures),
            "Resolve the failures above, confirm with 'devdock check', then retry.",
        )

    scope = ownership_scope(profile, root)
    if scope.compose_file is None:
        return _refusal(
            "UP_COMPOSE_FILE_MISSING",
            "Refusing to start: no Compose file is configured or discoverable.",
            (f"Project root: {scope.root}",),
            "Set compose_file in devdock.yaml or add a conventional Compose filename.",
        )
    if not resolves_inside(scope.compose_file, scope.root):
        return _refusal(
            "UP_COMPOSE_FILE_OUTSIDE_PROJECT",
            "Refusing to start: the Compose file does not resolve inside the project.",
            (f"Compose file: {scope.compose_file}",),
            "Remove the escaping symlink or choose a project-contained Compose file.",
        )
    if not scope.compose_file.is_file():
        return _refusal(
            "UP_COMPOSE_FILE_MISSING",
            "Refusing to start: the configured Compose file does not exist.",
            (f"Expected path: {scope.compose_file}",),
            "Create the file or update compose_file in devdock.yaml.",
        )

    capabilities = runtime.detect_capabilities()
    if not capabilities.compose_available:
        return _refusal(
            "UP_RUNTIME_UNAVAILABLE",
            "Refusing to start: the Docker Compose CLI is not available.",
            (),
            "Install Docker with the Compose plugin, then retry.",
        )
    if not capabilities.daemon_available:
        return _refusal(
            "UP_DAEMON_UNAVAILABLE",
            "Refusing to start: the Docker daemon did not respond.",
            (),
            "Start Docker Desktop or the Docker Engine service and retry.",
        )

    assessment = assess_ownership(scope, runtime.list_project_resources(scope.project_name))
    preview = assessment.explain()
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
        compose_file=scope.compose_file,
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
            + ("Containers may have been created before the timeout.",)
            + captured,
            "Run 'devdock up' again with a longer --timeout, or inspect the project with "
            "the docker ps command above.",
        )
    if outcome.status == "failed":
        return _refusal(
            "UP_FAILED",
            f"Docker Compose could not start the project (exit code {outcome.returncode}).",
            preview + captured,
            "Correct the error reported above, then retry. Use --show-output for the full "
            "runtime output.",
        )
    return LifecycleResult(
        "UP_STARTED",
        True,
        f"Started {scope.project_name}.",
        preview
        + captured
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
) -> tuple[str, ...]:
    """Return redacted runtime output: everything on request, otherwise a tail."""

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
    tail = summarize(stderr or stdout, environ)
    return tuple(f"  {line}" for line in tail)
