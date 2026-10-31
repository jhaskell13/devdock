"""Readiness behaviour under faults, against a real Docker daemon.

The unit tests drive the wait engine with an injected clock and scripted
observations, which proves the logic. These prove the premises that logic
rests on: that Docker reports health the way DevDock models it, that a
container which never becomes healthy really does stay that way, and that
DevDock's refusals and timeouts hold when a real runtime is on the other end.

Every project here is synthetic and disposable, uses images the rest of the
suite already needs, and is removed afterwards. Timings are generous: these
assert which outcome DevDock reaches, never how fast.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from helpers import docker, run_devdock

from devdock.adapters import PROJECT_LABEL
from devdock.config import load_profile
from devdock.discovery import CONFIG_NAME
from devdock.ownership import ownership_scope

pytestmark = pytest.mark.docker

Build = Callable[..., Path]

BUSYBOX = "busybox:stable"
IDLE = '["sh", "-c", "while true; do sleep 5; done"]'


def service(name: str, *, healthcheck: str = "", command: str = IDLE, depends: str = "") -> str:
    return f"  {name}:\n    image: {BUSYBOX}\n    command: {command}\n{depends}{healthcheck}"


def containers_of(root: Path) -> list[str]:
    """Container ids belonging to this project, and to no other.

    Every synthetic project writes the same ``project:`` into its profile, so
    they differ only by the digest DevDock derives from the directory. A
    ``--filter name=devdock-fault`` is a substring match and therefore picks
    up every other synthetic project too -- including, when the suite runs in
    parallel, ones another worker is still using. The project label is exact.
    """

    name = ownership_scope(load_profile(root / CONFIG_NAME), root).project_name
    found = docker("ps", "-aq", "--filter", f"label={PROJECT_LABEL}={name}")
    return found.stdout.split()


def health(test: str, *, start_period: str = "0s", retries: int = 1) -> str:
    return (
        "    healthcheck:\n"
        f'      test: ["CMD-SHELL", "{test}"]\n'
        "      interval: 1s\n"
        "      timeout: 2s\n"
        f"      retries: {retries}\n"
        f"      start_period: {start_period}\n"
    )


# --- a service that never becomes ready -----------------------------------


def test_a_service_whose_health_check_never_passes_times_out(synthetic: Build) -> None:
    """The timeout must name the service and show what it last saw."""

    root = synthetic("services:\n" + service("doomed", healthcheck=health("exit 1")))

    result = run_devdock(["up", "--ready-timeout", "8", "--deadline", "120"], root)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "UP_READINESS_TIMEOUT" in result.stderr
    assert "doomed" in result.stderr
    assert "compose health reports unhealthy" in result.stderr


def test_a_failing_probe_times_out_with_the_reason(synthetic: Build) -> None:
    """A port nothing listens on is the probe equivalent of the above."""

    root = synthetic(
        "services:\n" + service("quiet"),
        "schema_version: 1\n"
        "project: devdock-fault\n"
        "runtimes:\n  docker: compose\n"
        "compose_file: compose.yaml\n"
        "services:\n"
        "  quiet:\n"
        "    readiness:\n"
        "      - type: tcp\n"
        "        port: 9\n",
    )

    result = run_devdock(["up", "--ready-timeout", "6", "--deadline", "120"], root)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "UP_READINESS_TIMEOUT" in result.stderr
    assert "tcp 127.0.0.1:9" in result.stderr


# --- a service that becomes ready and then does not stay that way ---------


def test_a_service_that_goes_unhealthy_after_becoming_ready_fails_fast(
    synthetic: Build,
) -> None:
    """The dependents of a service that dies must not each wait out their budget.

    ``faulty`` is healthy until it writes its own tombstone, and ``anchor``
    takes long enough that the wait is still running when that happens.
    """

    root = synthetic(
        "services:\n"
        + service(
            "faulty",
            command='["sh", "-c", "sleep 6; touch /tmp/die; while true; do sleep 5; done"]',
            healthcheck=health("[ ! -f /tmp/die ]"),
        )
        + service(
            "anchor",
            command='["sh", "-c", "sleep 90"]',
            healthcheck=health("[ -f /tmp/never ]"),
        )
    )

    started = time.monotonic()
    result = run_devdock(["up", "--ready-timeout", "90", "--deadline", "120"], root)
    elapsed = time.monotonic() - started

    assert result.returncode == 1, result.stdout + result.stderr
    assert "UP_DEPENDENCY_REGRESSED" in result.stderr
    assert "faulty" in result.stderr
    assert elapsed < 90, "a regression must not wait out the per-service budget"


# --- a condition that can never be met ------------------------------------


def test_waiting_for_health_on_a_service_that_reports_none_is_named(
    synthetic: Build,
) -> None:
    """The check deferred from reading the Compose configuration, at runtime.

    A health check absent from the Compose file is not proof that a service
    has none, because its image may define one. Only a running container that
    reports no health at all is, and that is what this asserts. The dependency
    is declared in devdock.yaml so Compose never sees it and cannot object.
    """

    root = synthetic(
        "services:\n" + service("plain") + service("needy"),
        "schema_version: 1\n"
        "project: devdock-fault\n"
        "runtimes:\n  docker: compose\n"
        "compose_file: compose.yaml\n"
        "services:\n"
        "  needy:\n"
        "    depends_on:\n"
        "      plain: service_healthy\n",
    )

    result = run_devdock(["up", "--ready-timeout", "90", "--deadline", "120"], root)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "UP_CONDITION_UNSATISFIABLE" in result.stderr
    assert "reports no health status" in result.stderr


def test_waiting_for_completion_of_a_service_that_fails_is_named(
    synthetic: Build,
) -> None:
    root = synthetic(
        "services:\n"
        + service("failer", command='["sh", "-c", "exit 3"]')
        + service(
            "after",
            depends=(
                "    depends_on:\n"
                "      failer:\n"
                "        condition: service_completed_successfully\n"
            ),
        )
    )

    result = run_devdock(["up", "--ready-timeout", "90", "--deadline", "120"], root)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "failer" in result.stderr
    # Either verdict is correct and which one wins depends on how quickly the
    # exit is observed; both name the service and neither waits out a budget.
    assert any(code in result.stderr for code in ("UP_SERVICE_DIED", "UP_CONDITION_UNSATISFIABLE"))


# --- graph shapes ---------------------------------------------------------


def test_a_diamond_is_ordered_and_both_middles_share_a_layer(synthetic: Build) -> None:
    root = synthetic(
        "services:\n"
        + service("base", healthcheck=health("true"))
        + service("left", healthcheck=health("true"), depends="    depends_on: [base]\n")
        + service("right", healthcheck=health("true"), depends="    depends_on: [base]\n")
        + service("top", depends="    depends_on: [left, right]\n")
    )

    result = run_devdock(["up", "--deadline", "150"], root)

    assert result.returncode == 0, result.stdout + result.stderr
    text = result.stdout
    assert "Startup order (3 layers)" in text
    assert "  2. left, right" in text
    assert "  3. top" in text


def test_a_cycle_only_the_merged_graph_can_see_is_refused(synthetic: Build) -> None:
    """Neither source is cyclic alone, so only DevDock can catch this."""

    root = synthetic(
        "services:\n" + service("one") + service("two", depends="    depends_on: [one]\n"),
        "schema_version: 1\n"
        "project: devdock-fault\n"
        "runtimes:\n  docker: compose\n"
        "compose_file: compose.yaml\n"
        "services:\n"
        "  one:\n"
        "    depends_on:\n"
        "      two: service_started\n",
    )

    result = run_devdock(["up"], root)

    assert result.returncode == 1
    assert "UP_DEPENDENCY_CYCLE" in result.stderr
    assert containers_of(root) == [], "nothing may be created when the graph is refused"


# --- interruption ---------------------------------------------------------


def test_an_interrupt_during_a_wait_exits_130_and_removes_nothing(
    synthetic: Build,
) -> None:
    """The whole cancellation path, end to end, against a real daemon."""

    root = synthetic(
        "services:\n"
        + service("slow", command='["sh", "-c", "sleep 120"]', healthcheck=health("[ -f /no ]"))
    )

    process = subprocess.Popen(
        [sys.executable, "-m", "devdock", "up", "--deadline", "120", str(root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        time.sleep(20)
        os.killpg(process.pid, signal.SIGINT)
        output, _ = process.communicate(timeout=60)
    finally:
        if process.poll() is None:  # pragma: no cover - only if the signal was missed
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=30)

    assert process.returncode == 130, output
    assert "UP_INTERRUPTED" in output
    assert "Nothing was removed" in output
    # The container it did create is still there: an interrupt is not a teardown.
    assert containers_of(root), "an interrupt removed what it had started"
