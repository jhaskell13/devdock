"""Graph rules, including randomized graphs over fixed seeds.

The randomized tests use ``random.Random(seed)`` over a fixed range rather than
a property-testing dependency: the generated shapes are the same on every
machine and in CI, a failure names the seed that produced it, and the project
keeps its small dependency list.
"""

from __future__ import annotations

import random
from collections.abc import Mapping

import pytest

from devdock.config import DependencyCondition
from devdock.graph import (
    DependencyGraph,
    GraphProblems,
    build_graph,
)

Edges = Mapping[str, Mapping[str, DependencyCondition]]

SEEDS = range(100)


def graph_of(
    declared: list[str],
    compose: Edges | None = None,
    profile: Edges | None = None,
    *,
    without_healthcheck: frozenset[str] = frozenset(),
    optional_edges: frozenset[tuple[str, str]] = frozenset(),
) -> DependencyGraph:
    result = build_graph(
        declared,
        compose or {},
        profile or {},
        without_healthcheck=without_healthcheck,
        optional_edges=optional_edges,
    )
    assert isinstance(result, DependencyGraph), getattr(result, "explain", lambda: result)()
    return result


def problems_of(
    declared: list[str],
    compose: Edges | None = None,
    profile: Edges | None = None,
    *,
    without_healthcheck: frozenset[str] = frozenset(),
) -> GraphProblems:
    result = build_graph(
        declared, compose or {}, profile or {}, without_healthcheck=without_healthcheck
    )
    assert isinstance(result, GraphProblems), result
    return result


# --- ordering -----------------------------------------------------------


def test_services_without_dependencies_all_start_together() -> None:
    graph = graph_of(["web", "db", "cache"])

    assert graph.layers == (("cache", "db", "web"),)
    assert graph.dependencies == ()


def test_a_chain_produces_one_layer_per_link() -> None:
    graph = graph_of(
        ["web", "api", "db"],
        compose={"web": {"api": "service_started"}, "api": {"db": "service_started"}},
    )

    assert graph.layers == (("db",), ("api",), ("web",))


def test_a_diamond_puts_both_middle_services_in_one_layer() -> None:
    graph = graph_of(
        ["web", "left", "right", "db"],
        compose={
            "web": {"left": "service_started", "right": "service_started"},
            "left": {"db": "service_started"},
            "right": {"db": "service_started"},
        },
    )

    assert graph.layers == (("db",), ("left", "right"), ("web",))


def test_a_service_waits_for_its_deepest_dependency_not_its_nearest() -> None:
    """``web`` depends on both ``db`` and ``api``; it cannot share a layer with ``api``."""

    graph = graph_of(
        ["web", "api", "db"],
        compose={"web": {"api": "service_started", "db": "service_started"}, "api": {"db": "x"}},  # type: ignore[dict-item]
    )

    assert graph.layers == (("db",), ("api",), ("web",))


def test_layers_and_nodes_are_sorted_so_the_plan_does_not_move_between_runs() -> None:
    first = graph_of(["zebra", "apple", "mango"])
    second = graph_of(["mango", "zebra", "apple"])

    assert first.layers == second.layers == (("apple", "mango", "zebra"),)
    assert first.nodes == second.nodes


def test_an_empty_project_is_a_valid_empty_graph() -> None:
    graph = graph_of([])

    assert (graph.nodes, graph.layers, graph.dependencies) == ((), (), ())
    assert graph.explain() == ("Startup order: no services are declared.",)


def test_duplicate_declared_names_collapse_to_one_node() -> None:
    graph = graph_of(["web", "web", "db"])

    assert graph.nodes == ("db", "web")


# --- merging the two sources --------------------------------------------


def test_profile_dependencies_are_added_to_the_compose_ones() -> None:
    graph = graph_of(
        ["web", "db", "cache"],
        compose={"web": {"db": "service_started"}},
        profile={"web": {"cache": "service_started"}},
    )

    assert graph.layers == (("cache", "db"), ("web",))
    assert {item.depends_on for item in graph.dependencies_of("web")} == {"db", "cache"}


def test_an_edge_both_sources_declare_records_both_sources() -> None:
    graph = graph_of(
        ["web", "db"],
        compose={"web": {"db": "service_started"}},
        profile={"web": {"db": "service_started"}},
    )

    assert graph.dependencies[0].sources == ("compose", "profile")


@pytest.mark.parametrize(
    ("compose_condition", "profile_condition", "expected"),
    [
        ("service_started", "service_healthy", "service_healthy"),
        ("service_healthy", "service_started", "service_healthy"),
        ("service_started", "service_ready", "service_ready"),
        ("service_healthy", "service_ready", "service_ready"),
        ("service_ready", "service_healthy", "service_ready"),
        ("service_started", "service_completed_successfully", "service_completed_successfully"),
        ("service_completed_successfully", "service_started", "service_completed_successfully"),
    ],
)
def test_the_stricter_of_two_compatible_conditions_wins(
    compose_condition: DependencyCondition,
    profile_condition: DependencyCondition,
    expected: DependencyCondition,
) -> None:
    graph = graph_of(
        ["web", "db"],
        compose={"web": {"db": compose_condition}},
        profile={"web": {"db": profile_condition}},
        without_healthcheck=frozenset(),
    )

    assert graph.dependencies[0].condition == expected


@pytest.mark.parametrize("running_condition", ["service_healthy", "service_ready"])
def test_running_and_completed_conditions_on_one_edge_are_a_contradiction(
    running_condition: DependencyCondition,
) -> None:
    problems = problems_of(
        ["web", "db"],
        compose={"web": {"db": running_condition}},
        profile={"web": {"db": "service_completed_successfully"}},
        without_healthcheck=frozenset(),
    )

    assert problems.kinds == ("conflicting_conditions",)
    assert "cannot both hold" in problems.problems[0].summary
    assert any("compose requires" in line for line in problems.problems[0].detail)


# --- refusals ------------------------------------------------------------


def test_a_dependency_on_an_undeclared_service_names_both() -> None:
    problems = problems_of(["web"], compose={"web": {"db": "service_started"}})

    assert problems.kinds == ("undeclared_dependency",)
    assert "web depends on 'db'" in problems.problems[0].summary


def test_a_profile_service_the_compose_file_does_not_declare_is_reported() -> None:
    problems = problems_of(["web"], profile={"web": {}, "worker": {}})

    assert problems.kinds == ("undeclared_service",)
    assert "'worker'" in problems.problems[0].summary


def test_a_profile_service_with_no_dependencies_is_still_checked_for_existence() -> None:
    """The caller passes an entry per profile service, so a typo is caught."""

    assert problems_of(["web"], profile={"typo": {}}).kinds == ("undeclared_service",)


def test_waiting_for_health_on_a_service_that_disables_its_health_check_is_refused() -> None:
    problems = problems_of(
        ["web", "db"],
        compose={"web": {"db": "service_healthy"}},
        without_healthcheck=frozenset({"db"}),
    )

    assert problems.kinds == ("healthcheck_missing",)
    assert "never be met" in problems.problems[0].detail[0]


def test_health_conditions_are_accepted_when_the_service_has_a_health_check() -> None:
    graph = graph_of(
        ["web", "db"],
        compose={"web": {"db": "service_healthy"}},
        without_healthcheck=frozenset(),
    )

    assert graph.layers == (("db",), ("web",))


def test_a_health_check_merely_absent_from_the_compose_file_is_not_proof() -> None:
    """The image may define one, which the Compose configuration cannot show.

    Refusing here would reject projects that work. The wait reports it instead,
    once a running container shows whether health is reported at all.
    """

    graph = graph_of(["web", "db"], compose={"web": {"db": "service_healthy"}})

    assert graph.dependencies[0].condition == "service_healthy"


# --- optional dependencies ----------------------------------------------


def test_dependencies_are_required_by_default() -> None:
    graph = graph_of(["web", "db"], compose={"web": {"db": "service_started"}})

    assert graph.dependencies[0].required is True


def test_an_edge_compose_marks_not_required_is_advisory_and_says_so() -> None:
    graph = graph_of(
        ["web", "db"],
        compose={"web": {"db": "service_started"}},
        optional_edges=frozenset({("web", "db")}),
    )

    assert graph.dependencies[0].required is False
    assert graph.dependencies[0].describe().endswith("(service_started, from compose, optional)")


def test_an_optional_edge_still_orders_the_start() -> None:
    graph = graph_of(
        ["web", "db"],
        compose={"web": {"db": "service_started"}},
        optional_edges=frozenset({("web", "db")}),
    )

    assert graph.layers == (("db",), ("web",))


def test_declaring_an_optional_compose_edge_in_the_profile_makes_it_required() -> None:
    """Writing it in devdock.yaml is an explicit request, so it outranks the marking."""

    graph = graph_of(
        ["web", "db"],
        compose={"web": {"db": "service_started"}},
        profile={"web": {"db": "service_started"}},
        optional_edges=frozenset({("web", "db")}),
    )

    assert graph.dependencies[0].required is True


def test_a_two_service_cycle_shows_the_path() -> None:
    problems = problems_of(
        ["web", "db"],
        compose={"web": {"db": "service_started"}, "db": {"web": "service_started"}},
    )

    assert problems.kinds == ("cycle",)
    assert problems.problems[0].detail[0] == "db depends on web depends on db"


def test_a_self_dependency_from_the_compose_file_is_a_cycle() -> None:
    problems = problems_of(["web"], compose={"web": {"web": "service_started"}})

    assert problems.kinds == ("cycle",)
    assert problems.problems[0].detail[0] == "web depends on web"


def test_each_cyclic_group_is_reported_once() -> None:
    problems = problems_of(
        ["a", "b", "c", "d", "lonely"],
        compose={
            "a": {"b": "service_started"},
            "b": {"a": "service_started"},
            "c": {"d": "service_started"},
            "d": {"c": "service_started"},
        },
    )

    assert problems.kinds == ("cycle", "cycle")
    assert [problem.detail[0] for problem in problems.problems] == [
        "a depends on b depends on a",
        "c depends on d depends on c",
    ]


def test_a_cycle_the_profile_closes_over_compose_edges_is_found() -> None:
    """Neither source alone is cyclic; the merged graph is."""

    problems = problems_of(
        ["web", "api", "db"],
        compose={"web": {"api": "service_started"}, "api": {"db": "service_started"}},
        profile={"db": {"web": "service_started"}},
    )

    assert problems.kinds == ("cycle",)


def test_every_problem_is_reported_in_one_pass() -> None:
    problems = problems_of(
        ["web", "db", "a", "b"],
        compose={
            "web": {"missing": "service_started", "db": "service_healthy"},
            "a": {"b": "service_started"},
            "b": {"a": "service_started"},
        },
        profile={"ghost": {}},
        without_healthcheck=frozenset({"db"}),
    )

    assert set(problems.kinds) == {
        "undeclared_service",
        "undeclared_dependency",
        "healthcheck_missing",
        "cycle",
    }


def test_problem_explanations_are_indented_under_their_summary() -> None:
    lines = problems_of(["web"], compose={"web": {"db": "service_started"}}).explain()

    assert lines[0].startswith("web depends on")
    assert all(line.startswith("  ") for line in lines[1:])


# --- rendering -----------------------------------------------------------


def test_the_plan_lists_layers_and_the_origin_of_each_dependency() -> None:
    graph = graph_of(
        ["web", "db"],
        compose={"web": {"db": "service_started"}},
        profile={"web": {"db": "service_healthy"}},
        without_healthcheck=frozenset(),
    )

    assert graph.explain() == (
        "Startup order (2 layers):",
        "  1. db",
        "  2. web",
        "Dependencies (1):",
        "  web depends on db (service_healthy, from compose and profile)",
    )


def test_the_plan_says_so_when_nothing_constrains_the_order() -> None:
    assert "all services may start together" in graph_of(["web"]).explain()[-1]


def test_lookups_find_both_directions_of_an_edge() -> None:
    graph = graph_of(["web", "db"], compose={"web": {"db": "service_started"}})

    assert graph.dependencies_of("web")[0].depends_on == "db"
    assert graph.dependents_of("db")[0].service == "web"
    assert graph.dependencies_of("db") == ()
    assert (graph.layer_of("db"), graph.layer_of("web"), graph.layer_of("gone")) == (0, 1, None)


# --- randomized graphs ---------------------------------------------------


def _random_dag(
    rng: random.Random, size: int, *, connected: bool = False
) -> tuple[list[str], dict[str, dict[str, str]]]:
    """A DAG by construction: a service may only depend on an earlier name.

    ``connected`` guarantees at least one edge. Tests that are about edges
    used to skip the seeds that happened to produce none, which quietly meant
    three of every hundred ran no assertion at all.
    """

    names = [f"s{index:02d}" for index in range(size)]
    edges: dict[str, dict[str, str]] = {}
    for position, name in enumerate(names):
        candidates = names[:position]
        chosen = rng.sample(candidates, rng.randint(0, min(len(candidates), 3)))
        if chosen:
            edges[name] = dict.fromkeys(chosen, "service_started")
    if connected and not edges and size > 1:
        edges[names[1]] = {names[0]: "service_started"}
    rng.shuffle(names)
    return names, edges


@pytest.mark.parametrize("seed", SEEDS)
def test_any_acyclic_graph_produces_a_layering_that_respects_every_edge(seed: int) -> None:
    rng = random.Random(seed)
    names, edges = _random_dag(rng, rng.randint(1, 25))

    graph = graph_of(names, compose=edges)  # type: ignore[arg-type]

    assert sorted(name for layer in graph.layers for name in layer) == sorted(names)
    assert sum(len(layer) for layer in graph.layers) == len(names)
    for item in graph.dependencies:
        before = graph.layer_of(item.depends_on)
        after = graph.layer_of(item.service)
        assert before is not None and after is not None
        assert before < after, f"{item.describe()} is not ordered"


@pytest.mark.parametrize("seed", SEEDS)
def test_the_layering_is_as_tight_as_the_edges_allow(seed: int) -> None:
    """Every service sits one layer after its deepest dependency, never later."""

    rng = random.Random(seed)
    names, edges = _random_dag(rng, rng.randint(1, 25))

    graph = graph_of(names, compose=edges)  # type: ignore[arg-type]

    for name in graph.nodes:
        depths = [graph.layer_of(item.depends_on) for item in graph.dependencies_of(name)]
        expected = 1 + max((depth for depth in depths if depth is not None), default=-1)
        assert graph.layer_of(name) == expected


def _reachable_from(start: str, edges: Mapping[str, Mapping[str, str]]) -> set[str]:
    """Every service ``start`` depends on, directly or transitively."""

    seen: set[str] = set()
    pending = list(edges.get(start, {}))
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        pending.extend(edges.get(node, {}))
    return seen


@pytest.mark.parametrize("seed", SEEDS)
def test_closing_any_dependency_path_into_a_loop_is_detected(seed: int) -> None:
    """Point something a service already depends on back at that service."""

    rng = random.Random(seed)
    names, edges = _random_dag(rng, rng.randint(2, 25), connected=True)
    graph_of(names, compose=edges)  # type: ignore[arg-type]  # acyclic to begin with
    with_dependencies = [name for name in sorted(names) if _reachable_from(name, edges)]
    assert with_dependencies, "the generator must produce something to close into a loop"
    service = rng.choice(with_dependencies)
    target = rng.choice(sorted(_reachable_from(service, edges)))
    edges.setdefault(target, {})[service] = "service_started"

    problems = problems_of(names, compose=edges)  # type: ignore[arg-type]

    assert "cycle" in problems.kinds


@pytest.mark.parametrize("seed", SEEDS)
def test_the_result_does_not_depend_on_the_order_names_arrive_in(seed: int) -> None:
    rng = random.Random(seed)
    names, edges = _random_dag(rng, rng.randint(1, 25))
    shuffled = list(names)
    rng.shuffle(shuffled)

    first = graph_of(names, compose=edges)  # type: ignore[arg-type]
    second = graph_of(shuffled, compose=dict(reversed(list(edges.items()))))  # type: ignore[arg-type]

    assert first == second


def test_a_long_chain_is_rejected_without_exhausting_the_stack() -> None:
    """Deep nesting must produce a refusal, not a RecursionError."""

    size = 5000
    names = [f"s{index}" for index in range(size)]
    edges = {names[index]: {names[index - 1]: "service_started"} for index in range(1, size)}
    edges[names[0]] = {names[-1]: "service_started"}  # close the chain into one cycle

    problems = problems_of(names, compose=edges)  # type: ignore[arg-type]

    assert problems.kinds == ("cycle",)


def test_a_cycle_is_still_reported_when_one_of_its_edges_was_also_rejected() -> None:
    """An unsatisfiable condition does not hide the cycle the edge takes part in."""

    problems = problems_of(
        ["web", "db"],
        compose={"web": {"db": "service_healthy"}, "db": {"web": "service_started"}},
        without_healthcheck=frozenset({"db"}),
    )

    assert set(problems.kinds) == {"healthcheck_missing", "cycle"}
