"""Fixtures for the Docker integration suite.

Every test here needs a reachable Docker daemon and is marked ``docker`` so the
default test run deselects it. Each fixture creates only disposable local
resources inside a temporary directory and removes them in ``finally``,
including the volumes DevDock itself deliberately preserves.
"""

from __future__ import annotations

import itertools
import random
import shutil
import socket
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
EXAMPLE_PORT = "18080"

# Each test project publishes its own host port. Docker Desktop releases a host
# port asynchronously after `compose down`, so consecutive tests sharing one
# port raced each other into a genuine UP_PORT_CONFLICT. A per-session random
# base keeps reruns from colliding with ports a previous run has not yet freed.
_ports = itertools.count(random.randint(20000, 40000))


def _next_free_port() -> int:
    for candidate in _ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
        return candidate
    raise RuntimeError("unreachable")  # pragma: no cover


@pytest.fixture(scope="session")
def compose_cli() -> None:
    """Skip unless the Compose CLI responds.

    Weaker than ``docker_daemon`` on purpose: ``docker compose config`` only
    parses files, so the tests that check what DevDock extracts from a rendered
    configuration need no daemon and create nothing. They still live in the
    gated suite, because they need a real Compose to be worth anything.
    """

    if not ComposeAdapter().detect_capabilities().compose_available:
        pytest.skip("Docker Compose CLI is not available.")


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
    port = str(_next_free_port())
    for name in ("compose.yaml", "devdock.yaml"):
        path = root / name
        path.write_text(path.read_text(encoding="utf-8").replace(EXAMPLE_PORT, port), "utf-8")
    (root / ".devdock-test-port").write_text(port, encoding="utf-8")
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


@pytest.fixture
def host_port(project: Path) -> int:
    """The host port this test's web service publishes."""

    return int((project / ".devdock-test-port").read_text(encoding="utf-8"))
