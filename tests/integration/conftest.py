"""Fixtures for the Docker integration suite.

Every test here needs a reachable Docker daemon and is marked ``docker`` so the
default test run deselects it. Each fixture creates only disposable local
resources inside a temporary directory and removes them in ``finally``,
including the volumes DevDock itself deliberately preserves.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpers import compose_teardown, docker

from devdock.adapters import ComposeAdapter
from devdock.config import load_profile
from devdock.discovery import CONFIG_NAME
from devdock.failures import PERMISSION_DENIED_REMEDY
from devdock.ownership import OwnershipScope, ownership_scope

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "demo"


@pytest.fixture(scope="session")
def docker_daemon() -> None:
    """Skip the whole suite, with the reason, unless DevDock itself can reach the daemon."""

    capabilities = ComposeAdapter().detect_capabilities()
    if not capabilities.compose_available:
        pytest.skip("Docker Compose CLI is not available; run with Docker to exercise this suite.")
    if capabilities.daemon == "permission_denied":
        pytest.skip(f"This user may not connect to the Docker daemon. {PERMISSION_DENIED_REMEDY}")
    if not capabilities.daemon_available:
        pytest.skip("No reachable Docker daemon; start Docker and rerun with -m docker.")


def _scope_for(root: Path) -> OwnershipScope:
    return ownership_scope(load_profile(root / CONFIG_NAME), root)


@pytest.fixture
def project(docker_daemon: None, tmp_path: Path) -> Iterator[Path]:
    """A copy of the demo project at a unique path, fully cleaned up afterwards."""

    root = tmp_path / "demo"
    shutil.copytree(EXAMPLE, root)
    project_name = _scope_for(root).project_name
    try:
        yield root
    finally:
        compose_teardown(root, project_name)


@pytest.fixture
def scope(project: Path) -> OwnershipScope:
    return _scope_for(project)


@pytest.fixture
def leftovers() -> Iterator[list[str]]:
    """Track extra container names a test creates so they are always removed."""

    names: list[str] = []
    try:
        yield names
    finally:
        for name in names:
            docker("rm", "--force", "--volumes", name)
