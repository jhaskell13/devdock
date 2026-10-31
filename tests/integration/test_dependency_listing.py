"""What a real Compose actually renders, and what DevDock keeps from it.

The unit tests feed the adapter a document shaped like Compose's. These tests
let Compose produce it, because the whole safety argument for
``list_dependencies`` rests on claims about Compose's behavior that only
Compose can confirm: that ``--no-interpolate`` leaves ``${VAR}`` unexpanded,
that it stops ``env_file`` and ``.env`` values being rendered into the output,
and that the short dependency form is normalized away.

These need no daemon and create nothing. They are gated anyway, because
without a real Compose they would prove nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

from devdock.adapters import ComposeAdapter, DeclaredDependency
from devdock.graph import DependencyGraph, build_graph

pytestmark = pytest.mark.docker

COMPOSE_FILE = """
services:
  web:
    image: nginx:alpine
    environment:
      INTERPOLATED: ${DEVDOCK_TEST_SHELL_SECRET}
    env_file: [extra.env]
    volumes: ["./local-code:/app"]
    depends_on:
      db:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
      cache:
        condition: service_started
        required: false
  db:
    image: postgres:16
    healthcheck:
      test: ["CMD", "true"]
  migrate:
    image: busybox
  cache:
    image: redis
    healthcheck:
      disable: true
"""

# Values planted in every channel Compose reads from. None may appear in what
# DevDock keeps.
SHELL_SECRET = "shell-secret-cc9c1f"
ENV_FILE_SECRET = "env-file-secret-3a71bd"
DOT_ENV_SECRET = "dot-env-secret-5be204"


@pytest.fixture
def rendered_project(compose_cli: None, tmp_path: Path) -> Path:
    (tmp_path / "compose.yaml").write_text(COMPOSE_FILE, encoding="utf-8")
    (tmp_path / "extra.env").write_text(f"FROM_ENV_FILE={ENV_FILE_SECRET}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        f"DEVDOCK_TEST_SHELL_SECRET={DOT_ENV_SECRET}\nSPARE={DOT_ENV_SECRET}\n", encoding="utf-8"
    )
    (tmp_path / "local-code").mkdir()
    return tmp_path


def test_compose_renders_secrets_without_no_interpolate(rendered_project: Path) -> None:
    """The premise of the flag. If this ever stops being true, the flag is pointless."""

    rendered = subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            str(rendered_project / "compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        cwd=rendered_project,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            "DEVDOCK_TEST_SHELL_SECRET": SHELL_SECRET,
        },
    )

    assert rendered.returncode == 0, rendered.stderr[:400]
    assert SHELL_SECRET in rendered.stdout, "expected interpolation to expand the shell value"
    assert ENV_FILE_SECRET in rendered.stdout, "expected env_file to be resolved into the output"


def test_what_devdock_keeps_contains_none_of_those_secrets(
    rendered_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVDOCK_TEST_SHELL_SECRET", SHELL_SECRET)

    kept = repr(
        ComposeAdapter().list_dependencies(
            project_root=rendered_project,
            compose_file=rendered_project / "compose.yaml",
        )
    )

    for secret in (SHELL_SECRET, ENV_FILE_SECRET, DOT_ENV_SECRET):
        assert secret not in kept
    for incidental in ("nginx", "postgres", "extra.env", "local-code", "INTERPOLATED"):
        assert incidental not in kept


def test_a_real_configuration_yields_the_ordering_facts(
    rendered_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVDOCK_TEST_SHELL_SECRET", SHELL_SECRET)

    declared = ComposeAdapter().list_dependencies(
        project_root=rendered_project, compose_file=rendered_project / "compose.yaml"
    )

    assert declared.usable, (declared.error, declared.unsupported)
    assert declared.services == ("cache", "db", "migrate", "web")
    assert declared.healthcheck_declared == frozenset({"db"})
    assert declared.healthcheck_disabled == frozenset({"cache"})
    assert {(item.depends_on, item.condition, item.required) for item in declared.dependencies} == {
        ("db", "service_healthy", True),
        ("migrate", "service_completed_successfully", True),
        ("cache", "service_started", False),
    }


def test_a_real_configuration_builds_a_startup_order(
    rendered_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVDOCK_TEST_SHELL_SECRET", SHELL_SECRET)
    declared = ComposeAdapter().list_dependencies(
        project_root=rendered_project, compose_file=rendered_project / "compose.yaml"
    )

    graph = build_graph(
        declared.services,
        _edges(declared.dependencies),
        {},
        without_healthcheck=declared.healthcheck_disabled,
        optional_edges=frozenset(
            (item.service, item.depends_on) for item in declared.dependencies if not item.required
        ),
    )

    assert isinstance(graph, DependencyGraph), graph
    assert graph.layers == (("cache", "db", "migrate"), ("web",))


def test_compose_normalizes_the_short_dependency_form(compose_cli: None, tmp_path: Path) -> None:
    """Recorded so the list-form parser's reason for existing stays visible."""

    (tmp_path / "compose.yaml").write_text(
        "services:\n  web: {image: busybox, depends_on: [db]}\n  db: {image: busybox}\n",
        encoding="utf-8",
    )

    raw = subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            str(tmp_path / "compose.yaml"),
            "config",
            "--format",
            "json",
            "--no-interpolate",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    document = json.loads(raw.stdout)

    assert document["services"]["web"]["depends_on"] == {
        "db": {"condition": "service_started", "required": True}
    }
    declared = ComposeAdapter().list_dependencies(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )
    assert [(item.depends_on, item.condition) for item in declared.dependencies] == [
        ("db", "service_started")
    ]


def _edges(dependencies: Iterable[DeclaredDependency]) -> dict[str, dict[str, str]]:
    edges: dict[str, dict[str, str]] = {}
    for item in dependencies:
        edges.setdefault(item.service, {})[item.depends_on] = item.condition
    return edges
