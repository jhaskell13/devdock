"""Helpers shared by the Docker integration tests.

Kept out of ``conftest.py`` so tests can import them directly; pytest puts this
directory on ``sys.path`` because it contains no ``__init__.py``.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

COMMAND_TIMEOUT = 180.0


def docker(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a docker command directly, bypassing DevDock, for setup and teardown."""

    return subprocess.run(
        ["docker", *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT,
    )


def run_devdock(args: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI the way a user would, as a subprocess."""

    return subprocess.run(
        [sys.executable, "-m", "devdock", *args, str(root)],
        check=False,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT,
    )


def compose_teardown(root: Path, project_name: str, *, volumes: bool = True) -> None:
    """Remove everything a test project created, including volumes DevDock preserves."""

    docker(
        "compose",
        "--project-directory",
        str(root),
        "--file",
        str(root / "compose.yaml"),
        "--project-name",
        project_name,
        "down",
        *(["--volumes"] if volumes else []),
        "--remove-orphans",
    )
