"""The wait engine, driven by a fake clock and scripted observations.

No test here sleeps. ``FakeClock`` advances only when the engine asks to
sleep, so a sixty-second timeout is reached in a few hundred iterations of
arithmetic. That is the point of injecting the clock: the behaviours worth
testing are all about elapsed time, and none of them should cost any.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

import pytest

from devdock.config import DependencyCondition
from devdock.graph import DependencyGraph, build_graph
from devdock.orchestrate import (
    DEFAULT_DEADLINE,
    DEFAULT_READY_TIMEOUT,
    HEALTH_REPORT_GRACE,
    MAX_CONSECUTIVE_ERRORS,
    MAX_POLL_INTERVAL,
    CancelToken,
    Snapshot,
    WaitOutcome,
    condition_met,
    wait_for_ready,
)
from devdock.status import Readiness, ServiceStatus, SourceResult


class FakeClock:
    """A clock that only moves when the engine sleeps."""

    def __init__(self) -> None:
        self.time = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.time

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.time += seconds


def status(
    service: str,
    readiness: Readiness = "ready",
    *,
    sources: Sequence[SourceResult] = (),
    exit_code: int | None = None,
) -> ServiceStatus:
    return ServiceStatus(
        service=service,
        container=f"c-{service}",
        state="running" if readiness in {"ready", "running", "starting", "unhealthy"} else "exited",
        health="",
        exit_code=exit_code,
        ports=(),
        readiness=readiness,
        sources=tuple(sources),
        declared=True,
    )


def health(passed: bool) -> SourceResult:
    return SourceResult(
        "compose_health", "compose health", passed, f"reports {'healthy' if passed else 'starting'}"
    )


def tcp(passed: bool) -> SourceResult:
    return SourceResult(
        "tcp", "tcp 127.0.0.1:5432", passed, "accepted a connection" if passed else "refused"
    )


class Script:
    """Returns each snapshot in turn, repeating the last one forever."""

    def __init__(self, *snapshots: Snapshot) -> None:
        self._snapshots = list(snapshots)
        self.calls = 0

    def __call__(self) -> Snapshot:
        self.calls += 1
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]


def graph_for(
    services: Sequence[str],
    edges: dict[str, dict[str, DependencyCondition]] | None = None,
) -> DependencyGraph:
    built = build_graph(list(services), edges or {}, {})
    assert isinstance(built, DependencyGraph), built
    return built


def run(
    graph: DependencyGraph,
    observer: Script,
    **options: object,
) -> tuple[WaitOutcome, FakeClock]:
    clock = FakeClock()
    outcome = wait_for_ready(graph, observer, clock=clock, **options)  # type: ignore[arg-type]
    return outcome, clock


# --- the simple cases ---------------------------------------------------


def test_everything_already_ready_settles_on_the_first_look() -> None:
    script = Script(Snapshot((status("web", sources=[health(True)]),)))

    outcome, clock = run(graph_for(["web"]), script)

    assert outcome.status == "ready"
    assert outcome.summary == "All services are ready."
    assert (script.calls, clock.sleeps) == (1, [])


def test_a_service_with_nothing_to_check_settles_but_says_so() -> None:
    script = Script(Snapshot((status("web", "running"),)))

    outcome, _ = run(graph_for(["web"]), script)

    assert outcome.status == "unverified"
    assert outcome.ok
    assert outcome.unverified_services == ("web",)
    assert any("not that they are serving" in line for line in outcome.detail)


def test_require_ready_refuses_to_accept_an_unverifiable_service() -> None:
    script = Script(Snapshot((status("web", "running"),)))

    outcome, _ = run(graph_for(["web"]), script, require_ready=True, ready_timeout=10)

    assert outcome.status == "timed_out"


def test_a_slow_service_is_waited_for_and_then_accepted() -> None:
    script = Script(
        Snapshot((status("web", "starting", sources=[health(False)]),)),
        Snapshot((status("web", "starting", sources=[health(False)]),)),
        Snapshot((status("web", "ready", sources=[health(True)]),)),
    )

    outcome, clock = run(graph_for(["web"]), script)

    assert outcome.status == "ready"
    assert clock.time == pytest.approx(0.75)


def test_a_completed_one_shot_task_counts_as_settled() -> None:
    script = Script(Snapshot((status("migrate", "exited_ok", exit_code=0),)))

    outcome, _ = run(graph_for(["migrate"]), script)

    assert outcome.status == "ready"


# --- consecutive successes ----------------------------------------------


def test_a_probe_must_pass_twice_before_it_is_believed() -> None:
    """One accept can happen during a crash loop; a health check has its own retries."""

    script = Script(Snapshot((status("db", sources=[tcp(True)]),)))

    outcome, _ = run(graph_for(["db"]), script)

    assert outcome.status == "ready"
    assert script.calls == 2


def test_compose_health_is_believed_the_first_time() -> None:
    script = Script(Snapshot((status("db", sources=[health(True)]),)))

    outcome, _ = run(graph_for(["db"]), script)

    assert (outcome.status, script.calls) == ("ready", 1)


def test_a_probe_that_passes_then_fails_restarts_its_count() -> None:
    script = Script(
        Snapshot((status("db", sources=[tcp(True)]),)),
        Snapshot((status("db", "starting", sources=[tcp(False)]),)),
        Snapshot((status("db", sources=[tcp(True)]),)),
        Snapshot((status("db", sources=[tcp(True)]),)),
    )

    outcome, _ = run(graph_for(["db"]), script)

    assert (outcome.status, script.calls) == ("ready", 4)


# --- dependencies -------------------------------------------------------


def test_a_blocked_service_is_named_with_what_it_waits_on() -> None:
    script = Script(
        Snapshot(
            (
                status("db", "starting", sources=[health(False)]),
                status("web", "running"),
            )
        )
    )

    outcome, _ = run(
        graph_for(["db", "web"], {"web": {"db": "service_healthy"}}),
        script,
        ready_timeout=10,
    )

    assert outcome.status == "timed_out"
    assert any(
        "web: blocked, waiting on db (service_healthy)" in line for line in outcome.explain()
    )


def test_a_blocked_service_does_not_spend_its_own_budget() -> None:
    """Otherwise a service deep in the graph times out for its ancestors' slowness."""

    slow = [Snapshot((status("db", "starting", sources=[health(False)]), status("web", "running")))]
    slow *= 1
    script = Script(
        *slow,
        Snapshot((status("db", sources=[health(True)]), status("web", "running"))),
    )

    outcome, _ = run(
        graph_for(["db", "web"], {"web": {"db": "service_healthy"}}),
        script,
        ready_timeout=10,
    )

    assert outcome.status == "unverified"
    web = next(item for item in outcome.progress if item.service == "web")
    assert web.unblocked_at is not None and web.unblocked_at > 0


def test_a_diamond_waits_for_both_middle_services() -> None:
    graph = graph_for(
        ["db", "left", "right", "web"],
        {
            "left": {"db": "service_started"},
            "right": {"db": "service_started"},
            "web": {"left": "service_ready", "right": "service_ready"},
        },
    )
    script = Script(
        Snapshot(
            (
                status("db", sources=[health(True)]),
                status("left", sources=[health(True)]),
                status("right", "starting", sources=[health(False)]),
                status("web", "running"),
            )
        ),
        Snapshot(
            (
                status("db", sources=[health(True)]),
                status("left", sources=[health(True)]),
                status("right", sources=[health(True)]),
                status("web", "running"),
            )
        ),
    )

    outcome, _ = run(graph, script, ready_timeout=30)

    assert outcome.status == "unverified"


def test_an_optional_dependency_never_blocks() -> None:
    """Compose only warns when one is unavailable, so waiting would be stricter."""

    graph = build_graph(
        ["cache", "web"],
        {"web": {"cache": "service_started"}},
        {},
        optional_edges=frozenset({("web", "cache")}),
    )
    assert isinstance(graph, DependencyGraph)
    script = Script(Snapshot((status("cache", "missing"), status("web", sources=[health(True)]))))

    outcome, _ = run(graph, script, ready_timeout=10)

    # cache itself never arrives, so the wait still ends on its budget, but
    # web was never held back by it.
    web = next(item for item in outcome.progress if item.service == "web")
    assert web.blocked_by == ()
    assert web.reached_success


@pytest.mark.parametrize(
    ("condition", "readiness", "sources", "expected"),
    [
        ("service_started", "running", (), True),
        ("service_started", "ready", (), True),
        ("service_started", "missing", (), False),
        ("service_started", "exited_ok", (), True),
        ("service_healthy", "ready", (True,), True),
        ("service_healthy", "ready", (False,), False),
        ("service_healthy", "running", (), False),
        ("service_completed_successfully", "exited_ok", (), True),
        ("service_completed_successfully", "ready", (), False),
        ("service_ready", "ready", (True,), True),
        ("service_ready", "starting", (False,), False),
        ("service_ready", "running", (), True),
    ],
)
def test_each_condition_means_what_it_says(
    condition: DependencyCondition,
    readiness: Readiness,
    sources: tuple[bool, ...],
    expected: bool,
) -> None:
    from devdock.orchestrate import ServiceProgress

    item = ServiceProgress("db")
    item.readiness = readiness
    item.sources = tuple(health(passed) for passed in sources)
    item.ever_running = readiness in {"running", "ready", "starting", "unhealthy"}

    assert condition_met(condition, item) is expected


def test_service_ready_is_met_by_a_running_service_with_nothing_to_check() -> None:
    """The alternative is a wait that can never finish for an ordinary service."""

    graph = graph_for(["db", "web"], {"web": {"db": "service_ready"}})
    script = Script(Snapshot((status("db", "running"), status("web", "running"))))

    outcome, _ = run(graph, script, ready_timeout=10)

    assert outcome.status == "unverified"


# --- conditions that can never be met -----------------------------------


def test_waiting_for_health_on_a_service_reporting_none_is_called_out() -> None:
    graph = graph_for(["db", "web"], {"web": {"db": "service_healthy"}})
    script = Script(Snapshot((status("db", "running"), status("web", "running"))))

    outcome, clock = run(graph, script, ready_timeout=DEFAULT_READY_TIMEOUT)

    assert outcome.status == "unsatisfiable"
    assert any("reports no health status" in line for line in outcome.detail)
    assert clock.time < DEFAULT_READY_TIMEOUT, "should not have waited out the budget"


def test_a_service_is_given_a_grace_period_to_report_health_first() -> None:
    """The first health report can lag creation; absence is only proof after a while."""

    script = Script(
        Snapshot((status("db", "running"), status("web", "running"))),
        Snapshot((status("db", "running"), status("web", "running"))),
        Snapshot((status("db", "ready", sources=[health(True)]), status("web", "running"))),
    )

    outcome, clock = run(
        graph_for(["db", "web"], {"web": {"db": "service_healthy"}}), script, ready_timeout=30
    )

    assert outcome.status == "unverified"
    assert clock.time < HEALTH_REPORT_GRACE


def test_waiting_for_completion_of_a_service_that_failed_is_called_out() -> None:
    graph = graph_for(["migrate", "web"], {"web": {"migrate": "service_completed_successfully"}})
    script = Script(Snapshot((status("migrate", "stopped", exit_code=3), status("web", "running"))))

    outcome, _ = run(graph, script, ready_timeout=DEFAULT_READY_TIMEOUT)

    assert outcome.status == "unsatisfiable"
    assert any("exited with code 3" in line for line in outcome.detail)


# --- failure and regression ---------------------------------------------


def test_a_service_that_exits_before_becoming_ready_fails_the_wait() -> None:
    script = Script(Snapshot((status("web", "stopped", exit_code=1),)))

    outcome, _ = run(graph_for(["web"]), script)

    assert outcome.status == "failed"
    assert any("web exited with code 1" in line for line in outcome.detail)


def test_a_service_that_became_ready_and_then_died_is_a_regression() -> None:
    script = Script(
        Snapshot(
            (
                status("db", sources=[health(True)]),
                status("web", "starting", sources=[health(False)]),
            )
        ),
        Snapshot(
            (
                status("db", "stopped", exit_code=137),
                status("web", "starting", sources=[health(False)]),
            )
        ),
    )

    outcome, _ = run(
        graph_for(["db", "web"], {"web": {"db": "service_healthy"}}), script, ready_timeout=30
    )

    assert outcome.status == "regressed"
    assert any("exited with code 137" in line for line in outcome.detail)


def test_a_service_that_became_ready_and_then_unhealthy_is_a_regression() -> None:
    script = Script(
        Snapshot(
            (
                status("db", sources=[health(True)]),
                status("web", "starting", sources=[health(False)]),
            )
        ),
        Snapshot(
            (
                status("db", "unhealthy", sources=[health(False)]),
                status("web", "starting", sources=[health(False)]),
            )
        ),
    )

    outcome, _ = run(
        graph_for(["db", "web"], {"web": {"db": "service_healthy"}}), script, ready_timeout=30
    )

    assert outcome.status == "regressed"
    assert any("is now unhealthy" in line for line in outcome.detail)


def test_a_regression_fails_fast_rather_than_timing_every_dependent_out() -> None:
    script = Script(
        Snapshot(
            (
                status("db", sources=[health(True)]),
                status("a", "starting", sources=[health(False)]),
                status("b", "starting", sources=[health(False)]),
            )
        ),
        Snapshot(
            (
                status("db", "stopped", exit_code=1),
                status("a", "starting", sources=[health(False)]),
                status("b", "starting", sources=[health(False)]),
            )
        ),
    )
    graph = graph_for(
        ["db", "a", "b"],
        {"a": {"db": "service_healthy"}, "b": {"db": "service_healthy"}},
    )

    outcome, clock = run(graph, script, ready_timeout=DEFAULT_READY_TIMEOUT)

    assert outcome.status == "regressed"
    assert clock.time < DEFAULT_READY_TIMEOUT


def _flapping_script() -> Script:
    """``web`` loses readiness twice while ``slow`` keeps the wait open."""

    def pair(web_ready: bool, slow_ready: bool) -> Snapshot:
        return Snapshot(
            (
                status("web", "ready" if web_ready else "starting", sources=[health(web_ready)]),
                status("slow", "ready" if slow_ready else "starting", sources=[health(slow_ready)]),
            )
        )

    return Script(
        pair(True, False),
        pair(False, False),
        pair(True, False),
        pair(False, False),
        pair(True, True),
    )


def test_losing_readiness_without_failing_is_flapping_and_not_a_regression() -> None:
    """Dropping back to starting is instability, not a service that has gone away."""

    outcome, _ = run(graph_for(["web", "slow"]), _flapping_script(), ready_timeout=60)

    assert outcome.status == "ready"
    web = next(item for item in outcome.progress if item.service == "web")
    assert web.flaps == 2


def test_flapping_is_reported_in_the_explanation() -> None:
    outcome, _ = run(graph_for(["web", "slow"]), _flapping_script(), ready_timeout=60)

    assert any("lost it 2 time(s)" in line for line in outcome.explain())


def test_a_service_that_settles_on_the_first_look_stops_the_wait_immediately() -> None:
    """Nothing later in a script can matter once everything has settled."""

    script = Script(
        Snapshot((status("web", sources=[health(True)]),)),
        Snapshot((status("web", "stopped", exit_code=1),)),
    )

    outcome, _ = run(graph_for(["web"]), script)

    assert (outcome.status, script.calls) == ("ready", 1)


# --- time ---------------------------------------------------------------


def test_a_service_that_never_becomes_ready_times_out_on_its_own_budget() -> None:
    script = Script(Snapshot((status("db", "starting", sources=[tcp(False)]),)))

    outcome, clock = run(graph_for(["db"]), script, ready_timeout=20, deadline=600)

    assert outcome.status == "timed_out"
    assert clock.time == pytest.approx(20, abs=MAX_POLL_INTERVAL)
    assert any("db has been ready to start for" in line for line in outcome.detail)


def test_the_timeout_message_carries_the_last_probe_result() -> None:
    script = Script(Snapshot((status("db", "starting", sources=[tcp(False)]),)))

    outcome, _ = run(graph_for(["db"]), script, ready_timeout=10, deadline=600)

    assert any("Last result: tcp 127.0.0.1:5432 refused" in line for line in outcome.detail)


def test_the_global_deadline_overrides_a_remaining_per_service_budget() -> None:
    script = Script(Snapshot((status("db", "starting", sources=[tcp(False)]),)))

    outcome, clock = run(graph_for(["db"]), script, ready_timeout=600, deadline=30)

    assert outcome.status == "deadline_exceeded"
    assert clock.time == pytest.approx(30, abs=MAX_POLL_INTERVAL)
    assert any("Still not ready: db" in line for line in outcome.detail)


def test_polling_backs_off_instead_of_hammering_the_runtime() -> None:
    script = Script(Snapshot((status("db", "starting", sources=[tcp(False)]),)))

    _, clock = run(graph_for(["db"]), script, ready_timeout=60, deadline=600)

    assert clock.sleeps[0] == 0.25
    assert max(clock.sleeps) == MAX_POLL_INTERVAL
    assert clock.sleeps == sorted(clock.sleeps), "the interval should only grow"


def test_a_long_wait_costs_a_bounded_number_of_observations() -> None:
    """Backoff is what keeps a five-minute wait from being thousands of queries."""

    script = Script(Snapshot((status("db", "starting", sources=[tcp(False)]),)))

    run(graph_for(["db"]), script, ready_timeout=DEFAULT_DEADLINE, deadline=DEFAULT_DEADLINE)

    assert script.calls < 200


# --- cancellation and observation failures ------------------------------


def test_cancelling_stops_the_wait_without_touching_anything() -> None:
    token = CancelToken()
    token.cancel()
    script = Script(Snapshot((status("web", "starting", sources=[health(False)]),)))

    outcome, _ = run(graph_for(["web"]), script, cancel=token)

    assert outcome.status == "cancelled"
    assert script.calls == 0
    assert any("Nothing was stopped or removed" in line for line in outcome.detail)


def test_cancelling_midway_is_noticed_at_the_next_poll() -> None:
    token = CancelToken()

    class CancelAfterTwo(Script):
        def __call__(self) -> Snapshot:
            if self.calls >= 2:
                token.cancel()
            return super().__call__()

    script = CancelAfterTwo(Snapshot((status("web", "starting", sources=[health(False)]),)))

    outcome, _ = run(graph_for(["web"]), script, cancel=token, ready_timeout=600)

    assert outcome.status == "cancelled"
    assert script.calls == 3


def test_a_single_failed_observation_is_retried() -> None:
    script = Script(
        Snapshot(error="Service status query failed with exit code 1."),
        Snapshot((status("web", sources=[health(True)]),)),
    )

    outcome, _ = run(graph_for(["web"]), script)

    assert outcome.status == "ready"


def test_a_runtime_that_stops_answering_ends_the_wait() -> None:
    script = Script(Snapshot(error="Service status query failed with exit code 1."))

    outcome, _ = run(graph_for(["web"]), script, deadline=600)

    assert outcome.status == "unobservable"
    assert script.calls == MAX_CONSECUTIVE_ERRORS
    assert any("exit code 1" in line for line in outcome.detail)


def test_an_empty_project_is_immediately_ready() -> None:
    outcome, _ = run(graph_for([]), Script(Snapshot()))

    assert outcome.status == "ready"


def test_the_explanation_lists_every_service_and_its_sources() -> None:
    script = Script(
        Snapshot((status("db", sources=[health(True)]), status("web", sources=[tcp(True)]))),
    )

    outcome, _ = run(graph_for(["db", "web"]), script)
    text = "\n".join(outcome.explain())

    assert "db: ready" in text
    assert "[PASS] compose health reports healthy" in text
    assert "[PASS] tcp 127.0.0.1:5432 accepted a connection" in text


def test_several_services_failing_at_once_are_all_named() -> None:
    script = Script(
        Snapshot(
            (
                status("a", "stopped", exit_code=1),
                status("b", "stopped", exit_code=2),
                status("c", sources=[health(True)]),
            )
        )
    )

    outcome, _ = run(graph_for(["a", "b", "c"]), script)

    assert outcome.status == "failed"
    text = "\n".join(outcome.detail)
    assert "a exited with code 1" in text and "b exited with code 2" in text


# --- invariants over randomized orders ----------------------------------


def _random_layered_graph(rng: random.Random) -> tuple[DependencyGraph, list[str]]:
    names = [f"s{index}" for index in range(rng.randint(2, 10))]
    edges: dict[str, dict[str, DependencyCondition]] = {}
    for position, name in enumerate(names):
        for earlier in rng.sample(names[:position], rng.randint(0, min(position, 2))):
            edges.setdefault(name, {})[earlier] = rng.choice(
                ["service_started", "service_healthy", "service_ready"]
            )
    return graph_for(names, edges), names


class RandomRevealer:
    """Each service turns ready after its own randomly chosen number of polls."""

    def __init__(self, names: list[str], rng: random.Random) -> None:
        self._ready_after = {name: rng.randint(0, 6) for name in names}
        self.calls = 0

    def __call__(self) -> Snapshot:
        self.calls += 1
        return Snapshot(
            tuple(
                status(
                    name,
                    "ready" if self.calls > after else "starting",
                    sources=[health(self.calls > after)],
                )
                for name, after in self._ready_after.items()
            )
        )


@pytest.mark.parametrize("seed", range(60))
def test_a_successful_wait_means_every_declared_condition_is_met(seed: int) -> None:
    """The guarantee the engine makes, whatever order services come up in.

    Note what is deliberately *not* guaranteed: that a dependency settles
    before its dependent. A condition is a statement about the dependency
    alone -- ``service_healthy`` asks whether its health check passes, exactly
    as it does in Compose -- so a service whose own dependency is still
    pending can satisfy a condition for something else. What must hold is that
    nothing is reported ready with a condition outstanding.
    """

    rng = random.Random(seed)
    graph, names = _random_layered_graph(rng)

    outcome, _ = run(graph, RandomRevealer(names, rng), ready_timeout=600, deadline=600)

    assert outcome.status == "ready", outcome.explain()
    progress = {item.service: item for item in outcome.progress}
    assert all(not item.blocked_by for item in outcome.progress)
    for edge in graph.dependencies:
        assert condition_met(edge.condition, progress[edge.depends_on]), (
            f"{edge.describe()} was not met when the wait reported ready"
        )


@pytest.mark.parametrize("seed", range(60))
def test_a_wait_always_terminates_whatever_the_services_do(seed: int) -> None:
    """Random readiness churn must still reach a verdict inside the deadline."""

    rng = random.Random(seed)
    graph, names = _random_layered_graph(rng)
    choices: list[Readiness] = ["ready", "starting", "running", "unhealthy", "stopped"]

    class Chaos:
        def __call__(self) -> Snapshot:
            return Snapshot(
                tuple(
                    status(
                        name,
                        (picked := rng.choice(choices)),
                        sources=[health(picked == "ready")],
                        exit_code=1 if picked == "stopped" else None,
                    )
                    for name in names
                )
            )

    outcome, clock = run(graph, Chaos(), ready_timeout=45, deadline=120)  # type: ignore[arg-type]

    assert outcome.status in {
        "ready",
        "unverified",
        "timed_out",
        "deadline_exceeded",
        "failed",
        "regressed",
        "unsatisfiable",
    }
    assert clock.time <= 120 + MAX_POLL_INTERVAL


def test_a_service_that_never_appears_times_out_on_its_own_budget() -> None:
    """Not the global deadline: the answer is "web never arrived", not "the project is slow"."""

    script = Script(Snapshot((status("db", sources=[health(True)]),)))

    outcome, clock = run(graph_for(["db", "web"]), script, ready_timeout=15, deadline=600)

    assert outcome.status == "timed_out"
    assert "web did not become ready" in outcome.summary
    assert clock.time == pytest.approx(15, abs=MAX_POLL_INTERVAL)
    web = next(item for item in outcome.progress if item.service == "web")
    assert web.readiness == "missing"


def test_a_missing_service_behind_a_dependency_is_not_charged_for_the_wait() -> None:
    script = Script(
        Snapshot((status("db", "starting", sources=[health(False)]),)),
        Snapshot((status("db", "starting", sources=[health(False)]),)),
        Snapshot((status("db", sources=[health(True)]),)),
    )

    outcome, _ = run(
        graph_for(["db", "web"], {"web": {"db": "service_healthy"}}),
        script,
        ready_timeout=15,
        deadline=600,
    )

    web = next(item for item in outcome.progress if item.service == "web")
    assert outcome.status == "timed_out"
    assert web.unblocked_at is not None and web.unblocked_at > 0


# --- regressions found by the milestone audit ---------------------------


def test_a_service_whose_container_disappears_after_success_ends_the_wait() -> None:
    """Compose removes and recreates containers; the wait must not wedge on it."""

    script = Script(
        Snapshot(
            (
                status("web", sources=[health(True)]),
                status("slow", "starting", sources=[health(False)]),
            )
        ),
        Snapshot((status("web", "missing"), status("slow", "starting", sources=[health(False)]))),
    )

    outcome, clock = run(graph_for(["web", "slow"]), script, ready_timeout=600, deadline=600)

    assert outcome.status == "regressed"
    assert any("no longer exists" in line for line in outcome.detail)
    assert clock.time < 10


def test_a_flapping_dependency_cannot_make_a_timeout_unreachable() -> None:
    """Resetting the dependent's clock on every relapse made the budget infinite."""

    def pair(db_ready: bool) -> Snapshot:
        return Snapshot(
            (
                status("db", "ready" if db_ready else "starting", sources=[health(db_ready)]),
                status("web", "starting", sources=[health(False)]),
            )
        )

    class Oscillate:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> Snapshot:
            self.calls += 1
            return pair(self.calls % 2 == 1)

    graph = graph_for(["db", "web"], {"web": {"db": "service_ready"}})
    outcome, clock = run(graph, Oscillate(), ready_timeout=30, deadline=600)  # type: ignore[arg-type]

    assert outcome.status in {"timed_out", "unstable"}
    assert clock.time < 600, "the wait must end on a named cause, not the global deadline"


def test_waiting_for_health_on_a_completed_task_is_called_out() -> None:
    """The one-shot pattern with the wrong condition: it can never be healthy."""

    graph = graph_for(["migrate", "web"], {"web": {"migrate": "service_healthy"}})
    script = Script(
        Snapshot((status("migrate", "exited_ok", exit_code=0), status("web", "running")))
    )

    outcome, clock = run(graph, script, ready_timeout=600, deadline=600)

    assert outcome.status == "unsatisfiable"
    assert any("ran to completion" in line for line in outcome.detail)
    assert clock.time < 600


def test_waiting_for_readiness_of_a_completed_task_is_called_out() -> None:
    graph = graph_for(["migrate", "web"], {"web": {"migrate": "service_ready"}})
    script = Script(
        Snapshot((status("migrate", "exited_ok", exit_code=0), status("web", "running")))
    )

    outcome, _ = run(graph, script, ready_timeout=600, deadline=600)

    assert outcome.status == "unsatisfiable"
    assert any("service_completed_successfully instead" in line for line in outcome.detail)


def test_an_endlessly_flapping_service_is_called_unstable_not_slow() -> None:
    class Flap:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> Snapshot:
            self.calls += 1
            ready = self.calls % 2 == 1
            return Snapshot(
                (
                    status("web", "ready" if ready else "starting", sources=[health(ready)]),
                    status("slow", "starting", sources=[health(False)]),
                )
            )

    outcome, clock = run(graph_for(["web", "slow"]), Flap(), ready_timeout=600, deadline=600)  # type: ignore[arg-type]

    assert outcome.status == "unstable"
    assert any("lost it 3 times" in line for line in outcome.detail)
    assert clock.time < 600


def test_a_deadline_report_always_names_something_to_look_at() -> None:
    """It once rendered "Still not ready: ." with no service at all."""

    script = Script(Snapshot((status("web", "starting", sources=[health(False)]),)))

    outcome, _ = run(graph_for(["web"]), script, ready_timeout=600, deadline=20)

    assert outcome.status == "deadline_exceeded"
    assert "Still not ready: web." in outcome.detail


def test_a_crash_after_success_is_a_failure_not_a_flap() -> None:
    script = Script(
        Snapshot(
            (
                status("web", sources=[health(True)]),
                status("slow", "starting", sources=[health(False)]),
            )
        ),
        Snapshot(
            (
                status("web", "stopped", exit_code=1),
                status("slow", "starting", sources=[health(False)]),
            )
        ),
    )

    outcome, _ = run(graph_for(["web", "slow"]), script, ready_timeout=600, deadline=600)

    assert outcome.status == "regressed"
    web = next(item for item in outcome.progress if item.service == "web")
    assert web.flaps == 0, "dying once is not flapping"


def test_a_declared_ready_edge_does_not_drop_a_health_requirement() -> None:
    """Adding a profile line must not make an edge laxer than the Compose half.

    service_ready outranks service_healthy for a service that has a health
    check, but for one that has none it is weaker, so the merge alone would
    release the dependent against a service that was never healthy.
    """

    graph = build_graph(
        ["db", "web"],
        {"web": {"db": "service_healthy"}},
        {"web": {"db": "service_ready"}},
    )
    assert isinstance(graph, DependencyGraph)
    edge = graph.dependencies[0]
    assert (edge.condition, edge.requires_health) == ("service_ready", True)

    script = Script(Snapshot((status("db", "running"), status("web", "running"))))
    outcome, _ = run(graph, script, ready_timeout=600, deadline=600)

    assert outcome.status == "unsatisfiable"
    assert any("reports no health status" in line for line in outcome.detail)
