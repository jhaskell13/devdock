#!/usr/bin/env python3
"""Scripted DevDock walkthrough against a real Docker daemon.

Copies ``examples/demo`` to a temporary directory so the derived Compose
project name is unique to this run, then steps through the full lifecycle and
the safety behavior: ownership reporting, idempotent start and stop, a dry-run
preview, a refusal to touch a container DevDock did not create, and volume
preservation.

Everything it creates is removed at the end, including the volume DevDock
deliberately preserves. Run from the repository root:

    python scripts/demo.py            # full walkthrough, then clean up
    python scripts/demo.py --keep     # leave it running; prints the cleanup command
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
EXAMPLE = REPOSITORY / "examples" / "demo"
INTRUDER_SUFFIX = "-intruder-1"


class Stop(Exception):
    """A step failed in a way that makes continuing pointless."""


def heading(text: str) -> None:
    print(f"\n\033[1m=== {text} ===\033[0m" if sys.stdout.isatty() else f"\n=== {text} ===")


def devdock(*args: str, expect: int | None = 0) -> subprocess.CompletedProcess[str]:
    """Run the CLI, echo the command and its output, and check the exit code."""

    printed = " ".join(("devdock", *args))
    print(f"\n$ {printed}")
    result = subprocess.run(
        [sys.executable, "-m", "devdock", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    for stream in (result.stdout, result.stderr):
        if stream.strip():
            print(stream.rstrip())
    if expect is not None and result.returncode != expect:
        raise Stop(f"'{printed}' exited {result.returncode}, expected {expect}")
    return result


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def require_docker() -> None:
    if shutil.which("docker") is None:
        raise Stop("Docker is not on PATH. This demo needs a working Docker installation.")
    if docker("info", "--format", "{{.ServerVersion}}", check=False).returncode != 0:
        raise Stop("The Docker daemon is not responding. Start Docker Desktop or the service.")


def project_name(root: Path) -> str:
    result = devdock("inspect", str(root))
    for line in result.stdout.splitlines():
        if line.startswith("Compose project name: "):
            return line.split(": ", 1)[1].strip()
    raise Stop("Could not read the Compose project name from 'devdock inspect'.")


def wait_for_ready(root: Path, seconds: int = 60) -> None:
    """Poll status until the health-checked service reports ready."""

    print(f"\n(waiting up to {seconds}s for the web service's health check to pass)")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = subprocess.run(
            [sys.executable, "-m", "devdock", "status", str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
        if any(
            line.startswith("web ") and " ready " in line for line in result.stdout.splitlines()
        ):
            return
        time.sleep(2)
    print("(the health check did not pass in time; continuing anyway)")


def walkthrough(root: Path, keep: bool) -> str:
    heading("1. Discover the project and the resources DevDock will own")
    name = project_name(root)
    print(f"\n-> DevDock will manage the Compose project '{name}'.")
    print("   The digest comes from this directory, so a copy elsewhere is a different project.")

    heading("2. Check prerequisites (read-only)")
    devdock("check", str(root))

    heading("3. Status before anything is running")
    devdock("status", str(root))
    print("\n-> Every declared service is 'missing': declared, but no container exists.")

    heading("4. Start the services")
    devdock("up", str(root))
    print("\n-> Note the ownership scope printed before the action, and the reminder")
    print("   that services were started but readiness was NOT verified.")

    wait_for_ready(root)

    heading("5. Status once running")
    devdock("status", str(root))
    print("\n-> 'web' is ready (its health check passes).")
    print("   'worker' is 'running (readiness unverified)' because it declares no health check.")
    print("   DevDock never claims a service is ready when it cannot prove it.")

    heading("6. Start again: idempotent")
    devdock("up", str(root))
    print("\n-> Compose reconciles to the same state. The declared port 18080 is already")
    print("   bound by this project's own container, which is advisory, not a failure.")

    heading("7. Refuse to touch a container DevDock did not create")
    intruder = f"{name}{INTRUDER_SUFFIX}"
    docker("run", "--detach", "--name", intruder, "busybox:stable", "sleep", "300")
    print(f"\n(created '{intruder}' directly with docker run; it carries no Compose labels)")
    devdock("down", str(root), "--yes", expect=1)
    print("\n-> DevDock refuses: a container under its project name is not provably its own.")
    print("   It names the conflict and does nothing rather than guessing.")
    docker("rm", "--force", intruder)
    print(f"\n(removed '{intruder}')")

    heading("8. Preview the stop without changing anything")
    devdock("down", str(root), "--dry-run")
    print("\n-> The plan lists what would be removed and what would be preserved.")

    heading("9. Stop the project")
    devdock("down", str(root), "--yes")
    print("\n-> Containers and networks removed; the volume is preserved and named.")

    heading("10. Stop again: idempotent")
    devdock("down", str(root), "--yes")
    print("\n-> Nothing to stop, and no Docker command was run.")

    heading("11. The volume DevDock preserved")
    volumes = docker(
        "volume",
        "ls",
        "--filter",
        f"label=com.docker.compose.project={name}",
        "--format",
        "{{.Name}}",
    )
    print(f"\n$ docker volume ls --filter label=com.docker.compose.project={name}")
    print(volumes.stdout.rstrip() or "(none)")
    print("\n-> DevDock removes no volumes in this version. Data survives a teardown.")

    heading("12. Status after teardown")
    devdock("status", str(root))
    print("\n-> Services are 'missing' again, and the report is still available.")

    if keep:
        print(f"\nLeaving the project in place at {root}")
    return name


def cleanup(root: Path, name: str | None) -> None:
    if name:
        docker("rm", "--force", f"{name}{INTRUDER_SUFFIX}", check=False)
        subprocess.run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(root),
                "--file",
                str(root / "compose.yaml"),
                "--project-name",
                name,
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Leave the demo project running instead of cleaning it up.",
    )
    options = parser.parse_args()

    name: str | None = None
    root = Path(tempfile.mkdtemp(prefix="devdock-demo-"))
    try:
        require_docker()
        shutil.copytree(EXAMPLE, root, dirs_exist_ok=True)
        name = walkthrough(root, options.keep)
    except Stop as stop:
        print(f"\nStopped: {stop}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        if options.keep and name:
            print(
                "\nClean up with:\n"
                f"  docker compose --project-directory {root} "
                f"--file {root / 'compose.yaml'} --project-name {name} "
                "down --volumes --remove-orphans\n"
                f"  rm -rf {root}"
            )
        else:
            cleanup(root, name)
            print("\nCleaned up: containers, network, volume, and the temporary directory.")

    heading("Demo complete")
    print("Started, queried, and stopped repeatedly; ownership reported before every action.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
