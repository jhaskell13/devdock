"""DevDock command-line interface."""

from pathlib import Path
from typing import Annotated

import typer

from devdock.config import ProfileError, load_profile
from devdock.diagnostics import check_profile
from devdock.discovery import COMPOSE_NAMES, CONFIG_NAME, discover_project_result

app = typer.Typer(
    name="devdock",
    help="Inspect and check local development environments.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Project directory to initialize.")] = Path("."),
) -> None:
    """Create a starter DevDock profile in a project directory."""

    root = directory.resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        typer.echo(f"Could not create project directory {root}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    profile_path = root / CONFIG_NAME
    starter_profile = "\n".join(
        (
            "schema_version: 1",
            f"project: {root.name or 'my-project'}",
            "runtimes: {}",
            "compose_file: null",
            "required_env: []",
            "",
        )
    )
    try:
        with profile_path.open("x", encoding="utf-8") as profile_file:
            profile_file.write(starter_profile)
    except FileExistsError as exc:
        typer.echo(f"Profile already exists: {profile_path}", err=True)
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        typer.echo(f"Could not write profile {profile_path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Created {profile_path}")


@app.command()
def inspect(
    directory: Annotated[Path, typer.Argument(help="Directory to inspect.")] = Path("."),
) -> None:
    """Discover the project root and report common environment files."""

    discovery = discover_project_result(directory)
    root = discovery.root
    typer.echo(f"Project root: {root}")
    typer.echo(f"Discovery evidence: {', '.join(discovery.matched_markers) or 'no marker; used start directory'}")
    if discovery.ambiguous:
        typer.echo(
            "Nested project roots detected; using the nearest root. "
            "Pass a directory inside the intended project to select it."
        )
    profile = root / CONFIG_NAME
    typer.echo(f"DevDock profile: {'found' if profile.is_file() else 'missing'}")
    compose_files = [name for name in COMPOSE_NAMES if (root / name).is_file()]
    if compose_files:
        for name in compose_files:
            typer.echo(f"Compose file: {name}")
    else:
        typer.echo("Compose file: none found")
    typer.echo(f"Known project files: {', '.join(discovery.known_files) or 'none found'}")
    if profile.is_file():
        try:
            loaded = load_profile(profile)
        except ProfileError as exc:
            typer.echo(f"Profile error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"Configured project: {loaded.project}")
        typer.echo(f"Declared runtimes: {', '.join(loaded.runtimes) or 'none'}")


@app.command()
def check(
    directory: Annotated[Path, typer.Argument(help="Project directory to check.")] = Path("."),
) -> None:
    """Validate the project profile and check its declared prerequisites."""

    discovery = discover_project_result(directory)
    root = discovery.root
    profile_path = root / CONFIG_NAME
    if not profile_path.is_file():
        typer.echo(f"FAIL: {CONFIG_NAME} was not found in {root}", err=True)
        typer.echo("Run 'devdock init' to create a starter profile.", err=True)
        raise typer.Exit(code=1)
    try:
        profile = load_profile(profile_path)
    except ProfileError as exc:
        typer.echo(f"FAIL: Invalid {CONFIG_NAME}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    findings = check_profile(profile, root)
    failures = 0
    for finding in findings:
        typer.echo(f"[{finding.level}] {finding.message}")
        if finding.remedy:
            typer.echo(f"       Suggested action: {finding.remedy}")
        if finding.level == "FAIL":
            failures += 1
    if failures:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()

