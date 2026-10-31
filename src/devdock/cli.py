"""DevDock command-line interface."""

import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Annotated

import typer
import yaml

from devdock import __version__
from devdock.adapters.compose import START_TIMEOUT, STOP_TIMEOUT
from devdock.cancellation import CancelToken
from devdock.config import ProfileError, ProjectProfile, load_profile
from devdock.diagnostics import check_profile
from devdock.discovery import COMPOSE_NAMES, CONFIG_NAME, discover_project_result
from devdock.lifecycle import (
    LifecycleResult,
    execute_stop,
    plan_stop,
    project_status,
    start_project,
)
from devdock.metadata import read_python_metadata
from devdock.ownership import ownership_scope
from devdock.redaction import sanitize

app = typer.Typer(
    name="devdock",
    no_args_is_help=True,
    add_completion=False,
)


def _echo(message: str = "", *, err: bool = False) -> None:
    """Print one line with control characters neutralized."""

    typer.echo(sanitize(message), err=err)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"devdock {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_show_version, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    """Inspect, check, start, and stop local development environments."""


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Project directory to initialize.")] = Path("."),
) -> None:
    """Create a starter DevDock profile in a project directory."""

    root = directory.resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _echo(f"Could not create project directory {root}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    profile_path = root / CONFIG_NAME
    # Serialized rather than templated: a directory named "true", "1.0", or
    # "a: b" would otherwise produce a profile whose project is not a string.
    starter_profile = yaml.safe_dump(
        {
            "schema_version": 1,
            "project": root.name or "my-project",
            "runtimes": {},
            "compose_file": None,
            "required_env": [],
        },
        sort_keys=False,
        allow_unicode=True,
    )
    try:
        with profile_path.open("x", encoding="utf-8") as profile_file:
            profile_file.write(starter_profile)
    except FileExistsError as exc:
        _echo(f"Profile already exists: {profile_path}", err=True)
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        _echo(f"Could not write profile {profile_path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _echo(f"Created {profile_path}")


@app.command()
def inspect(
    directory: Annotated[Path, typer.Argument(help="Directory to inspect.")] = Path("."),
) -> None:
    """Discover the project root and report common environment files."""

    discovery = discover_project_result(directory)
    root = discovery.root
    _echo(f"Project root: {root}")
    evidence = ", ".join(discovery.matched_markers) or "no marker; used start directory"
    _echo(f"Discovery evidence: {evidence}")
    if discovery.ambiguous:
        _echo(
            "Nested project roots detected; using the nearest root. "
            "Pass a directory inside the intended project to select it."
        )
    profile = root / CONFIG_NAME
    _echo(f"DevDock profile: {'found' if profile.is_file() else 'missing'}")
    compose_files = [name for name in COMPOSE_NAMES if (root / name).is_file()]
    if compose_files:
        for name in compose_files:
            _echo(f"Compose file: {name}")
    else:
        _echo("Compose file: none found")
    _echo(f"Known project files: {', '.join(discovery.known_files) or 'none found'}")
    python_metadata = read_python_metadata(root)
    for declaration in python_metadata.declarations:
        _echo(f"Python project metadata: {declaration.source} = {declaration.value}")
    for error in python_metadata.errors:
        _echo(f"Python project metadata warning: {error}")
    if profile.is_file():
        try:
            loaded = load_profile(profile)
        except ProfileError as exc:
            _echo(f"Profile error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        _echo(f"Configured project: {loaded.project}")
        _echo(f"Declared runtimes: {', '.join(loaded.runtimes) or 'none'}")
        _echo(f"Compose project name: {ownership_scope(loaded, root).project_name}")


def _load_project(directory: Path) -> tuple[Path, ProjectProfile]:
    """Resolve the project root and its valid profile, or exit with an error."""

    root = discover_project_result(directory).root
    profile_path = root / CONFIG_NAME
    if not profile_path.is_file():
        _echo(f"FAIL: {CONFIG_NAME} was not found in {root}", err=True)
        _echo("Run 'devdock init' to create a starter profile.", err=True)
        raise typer.Exit(code=1)
    try:
        return root, load_profile(profile_path)
    except ProfileError as exc:
        _echo(f"FAIL: Invalid {CONFIG_NAME}: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _interactive() -> bool:
    """Whether a person can answer a confirmation prompt on this standard input."""

    return sys.stdin.isatty()


# POSIX shells report a signal-terminated process as 128 plus the signal
# number. 130 is the conventional result of an interrupt, and DevDock uses it
# on every platform so a script sees one answer rather than one per operating
# system.
EXIT_INTERRUPTED = 130

# Ctrl+Break, which only Windows raises. Handled alongside Ctrl+C so a user
# who reaches for it gets the same orderly stop rather than an abrupt one.
_BREAK_SIGNAL = getattr(signal, "SIGBREAK", None)


@contextmanager
def interruptible() -> Iterator[CancelToken]:
    """Turn an interrupt into a cancellation for the duration of one operation.

    The first interrupt asks the work to stop, so the command can finish
    reporting and leave the project in a state it has described. A second one
    exits immediately with the same status, for anyone who does not want to
    wait for the first to take effect.

    Handlers are installed only for as long as they are wanted and the
    previous ones are always restored, so DevDock does not change how an
    interrupt behaves anywhere else. Installing them requires the main thread;
    where that is not available the token is simply never set by a signal,
    which degrades to the behavior before any of this existed.
    """

    token = CancelToken()
    signals = [signal.SIGINT] + ([_BREAK_SIGNAL] if _BREAK_SIGNAL is not None else [])

    def on_signal(signum: int, frame: object) -> None:
        if token.cancelled:
            # SystemExit rather than KeyboardInterrupt: Click converts a
            # KeyboardInterrupt into "Aborted!" and exit 1, which would hide
            # both the reason and the status.
            raise SystemExit(EXIT_INTERRUPTED)
        token.cancel()
        # No embedded newline: output is sanitized, which would render it as "?".
        _echo("Stopping. Press Ctrl+C again to exit at once.", err=True)

    previous: list[tuple[int, object]] = []
    try:
        for number in signals:
            try:
                previous.append((number, signal.signal(number, on_signal)))
            except (ValueError, OSError):  # not the main thread, or not supported here
                continue
        yield token
    finally:
        for number, handler in previous:
            with suppress(ValueError, OSError):
                signal.signal(number, handler)  # type: ignore[arg-type]


def _render(result: LifecycleResult) -> None:
    """Print a lifecycle result and exit non-zero when it did not succeed."""

    stream_err = not result.ok
    _echo(f"[{'OK' if result.ok else 'REFUSED'}] {result.code}: {result.summary}", err=stream_err)
    for line in result.lines:
        _echo(line, err=stream_err)
    if result.remedy:
        _echo(f"Suggested action: {result.remedy}", err=stream_err)
    if not result.ok:
        # An interrupt is reported as itself rather than as a failure, so a
        # script can tell "you stopped this" from "this did not work".
        raise typer.Exit(code=EXIT_INTERRUPTED if result.code.endswith("_INTERRUPTED") else 1)


@app.command()
def up(
    directory: Annotated[Path, typer.Argument(help="Project directory to start.")] = Path("."),
    show_output: Annotated[
        bool,
        typer.Option("--show-output", help="Show the runtime's full output, still redacted."),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=1.0, help="Seconds to allow the start to run."),
    ] = START_TIMEOUT,
) -> None:
    """Start the project's Compose services detached, without waiting for readiness."""

    root, profile = _load_project(directory)
    with interruptible() as cancel:
        _render(
            start_project(profile, root, timeout=timeout, show_output=show_output, cancel=cancel)
        )


@app.command()
def down(
    directory: Annotated[Path, typer.Argument(help="Project directory to stop.")] = Path("."),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would change and exit without acting.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Proceed without an interactive confirmation.")
    ] = False,
    show_output: Annotated[
        bool,
        typer.Option("--show-output", help="Show the runtime's full output, still redacted."),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=1.0, help="Seconds to allow the stop to run."),
    ] = STOP_TIMEOUT,
) -> None:
    """Stop and remove the project's containers and networks. Volumes are never removed."""

    root, profile = _load_project(directory)
    plan = plan_stop(profile, root)
    if isinstance(plan, LifecycleResult):
        _render(plan)
        return
    for line in plan.explain():
        _echo(line)
    if dry_run:
        _echo("[OK] DOWN_DRY_RUN: Nothing was changed.")
        return
    if plan.nothing_to_do:
        _render(execute_stop(plan, timeout=timeout, show_output=show_output))
        return
    if not yes:
        if not _interactive():
            _echo(
                "[REFUSED] DOWN_CONFIRMATION_REQUIRED: Not an interactive terminal; "
                "pass --yes to proceed or --dry-run to preview.",
                err=True,
            )
            raise typer.Exit(code=1)
        if not typer.confirm("Stop and remove the resources listed above?", default=False):
            _echo("[REFUSED] DOWN_CANCELLED: No changes were made.", err=True)
            raise typer.Exit(code=1)
    with interruptible() as cancel:
        _render(execute_stop(plan, timeout=timeout, show_output=show_output, cancel=cancel))


@app.command()
def status(
    directory: Annotated[Path, typer.Argument(help="Project directory to report on.")] = Path("."),
) -> None:
    """Report state, health, readiness, and published ports for each service."""

    root, profile = _load_project(directory)
    _render(project_status(profile, root))


@app.command()
def check(
    directory: Annotated[Path, typer.Argument(help="Project directory to check.")] = Path("."),
) -> None:
    """Validate the project profile and check its declared prerequisites."""

    root, profile = _load_project(directory)

    findings = check_profile(profile, root)
    failures = 0
    for finding in findings:
        _echo(f"[{finding.severity}] {finding.code}: {finding.summary}")
        for evidence in finding.evidence:
            _echo(f"       Evidence: {evidence}")
        if finding.remedy:
            _echo(f"       Suggested action: {finding.remedy}")
        if finding.severity == "FAIL":
            failures += 1
    if failures:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
