"""The merged service dependency graph and the startup order derived from it.

Two sources declare dependencies: the Compose file, and the profile's
``services`` block. This module merges them, rejects the graphs that cannot
produce a startup order, and returns the layers a start should follow.

Everything here is a pure function over plain mappings. It never runs a
command, reads a file, or knows what a Compose file looks like; the caller
collects the edges and this module decides what they mean. That is what lets
the hard cases -- cycles, diamonds, contradictory conditions -- be tested
exhaustively in milliseconds.

Problems are returned as structured kinds rather than CLI codes, so the command
that builds the graph owns the vocabulary the user sees.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from devdock.config import DependencyCondition

DependencySource = Literal["compose", "profile"]
GraphProblemKind = Literal[
    "undeclared_service",
    "undeclared_dependency",
    "conflicting_conditions",
    "healthcheck_missing",
    "cycle",
]

# How strict each condition is along the "service is alive" axis. A stricter
# requirement implies every weaker one, so merging two of these keeps the
# strictest. ``service_completed_successfully`` is deliberately absent: it is
# not a point on this axis but a separate requirement that the service has
# already exited, so it is merged by the rules in ``_merge_conditions``.
_LIVENESS_RANK: dict[DependencyCondition, int] = {
    "service_started": 0,
    "service_healthy": 1,
    "service_ready": 2,
}
_COMPLETION: DependencyCondition = "service_completed_successfully"


def _merge_conditions(
    first: DependencyCondition, second: DependencyCondition
) -> DependencyCondition | None:
    """Combine two conditions on one edge, or ``None`` when they contradict.

    Both sources are honored rather than one overriding the other: an edge the
    Compose file and the profile both declare must satisfy both. Within the
    liveness axis the stricter condition implies the weaker one, so it wins.
    ``service_completed_successfully`` absorbs ``service_started``, because a
    service that exited successfully did start. It contradicts ``service_healthy``
    and ``service_ready``, which both require the service to still be running,
    and a contradiction is reported rather than silently resolved.
    """

    if first == second:
        return first
    if first in _LIVENESS_RANK and second in _LIVENESS_RANK:
        return max(first, second, key=lambda value: _LIVENESS_RANK[value])
    other = second if first == _COMPLETION else first
    if other == "service_started":
        return _COMPLETION
    return None


@dataclass(frozen=True)
class Dependency:
    """One edge: ``service`` may not start until ``depends_on`` meets ``condition``."""

    service: str
    depends_on: str
    condition: DependencyCondition
    sources: tuple[DependencySource, ...]

    def describe(self) -> str:
        origin = " and ".join(self.sources)
        return f"{self.service} depends on {self.depends_on} ({self.condition}, from {origin})"


@dataclass(frozen=True)
class GraphProblem:
    """A reason the declared dependencies cannot produce a startup order."""

    kind: GraphProblemKind
    summary: str
    detail: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphProblems:
    """Every problem found in one pass, so the user fixes them together."""

    problems: tuple[GraphProblem, ...]

    @property
    def kinds(self) -> tuple[GraphProblemKind, ...]:
        return tuple(problem.kind for problem in self.problems)

    def explain(self) -> tuple[str, ...]:
        lines: list[str] = []
        for problem in self.problems:
            lines.append(problem.summary)
            lines.extend(f"  {line}" for line in problem.detail)
        return tuple(lines)


@dataclass(frozen=True)
class DependencyGraph:
    """A validated graph and the order a start should follow.

    ``layers`` is the only ordering DevDock promises: every service in a layer
    may be started once every earlier layer has met its conditions. Services
    within a layer are independent of each other and are listed in sorted order
    so the plan a user sees does not move between runs.
    """

    nodes: tuple[str, ...]
    dependencies: tuple[Dependency, ...]
    layers: tuple[tuple[str, ...], ...]

    def dependencies_of(self, service: str) -> tuple[Dependency, ...]:
        """Edges ``service`` is waiting on."""

        return tuple(item for item in self.dependencies if item.service == service)

    def dependents_of(self, service: str) -> tuple[Dependency, ...]:
        """Edges waiting on ``service``."""

        return tuple(item for item in self.dependencies if item.depends_on == service)

    def layer_of(self, service: str) -> int | None:
        for position, layer in enumerate(self.layers):
            if service in layer:
                return position
        return None

    def explain(self) -> tuple[str, ...]:
        """The startup plan, printed before a start acts on it."""

        if not self.nodes:
            return ("Startup order: no services are declared.",)
        count = len(self.layers)
        lines = [f"Startup order ({count} layer{'' if count == 1 else 's'}):"]
        for position, layer in enumerate(self.layers, start=1):
            lines.append(f"  {position}. {', '.join(layer)}")
        if self.dependencies:
            lines.append(f"Dependencies ({len(self.dependencies)}):")
            lines.extend(f"  {item.describe()}" for item in self.dependencies)
        else:
            lines.append("Dependencies: none declared; all services may start together.")
        return tuple(lines)


def build_graph(
    declared: Sequence[str],
    compose_dependencies: Mapping[str, Mapping[str, DependencyCondition]],
    profile_dependencies: Mapping[str, Mapping[str, DependencyCondition]],
    *,
    healthchecked: frozenset[str] | None = None,
) -> DependencyGraph | GraphProblems:
    """Merge the declared dependencies into a startup order, or explain why not.

    ``declared`` is the authoritative set of services, which is the Compose
    file's. ``profile_dependencies`` must contain an entry for every service the
    profile names, including those that declare no dependency, so that a profile
    naming a service the Compose file does not have is reported.

    ``healthchecked`` lists the services that have a Compose health check. Pass
    ``None`` when that is not known -- for example when the Compose
    configuration could not be read -- and the check that ``service_healthy`` is
    satisfiable is skipped rather than guessed at.
    """

    nodes = tuple(sorted(set(declared)))
    known = set(nodes)
    problems: list[GraphProblem] = []

    for name in sorted(set(profile_dependencies) - known):
        problems.append(
            GraphProblem(
                "undeclared_service",
                f"The profile configures service {name!r}, which the Compose file does not "
                "declare.",
                ("Correct the name in devdock.yaml, or add the service to the Compose file.",),
            )
        )

    merged, merge_problems = _merge_edges(
        known, compose_dependencies, profile_dependencies, healthchecked
    )
    problems.extend(merge_problems)
    problems.extend(_cycle_problems(nodes, merged))
    if problems:
        return GraphProblems(tuple(problems))
    return DependencyGraph(nodes, merged, _layers(nodes, merged))


def _merge_edges(
    known: set[str],
    compose_dependencies: Mapping[str, Mapping[str, DependencyCondition]],
    profile_dependencies: Mapping[str, Mapping[str, DependencyCondition]],
    healthchecked: frozenset[str] | None,
) -> tuple[tuple[Dependency, ...], list[GraphProblem]]:
    """Combine both sources edge by edge and validate each merged edge."""

    collected: dict[tuple[str, str], tuple[DependencyCondition, list[DependencySource]]] = {}
    problems: list[GraphProblem] = []
    pairs: list[tuple[DependencySource, Mapping[str, Mapping[str, DependencyCondition]]]] = [
        ("compose", compose_dependencies),
        ("profile", profile_dependencies),
    ]
    for source, declarations in pairs:
        for service in sorted(declarations):
            if service not in known:
                continue  # already reported, or not a service this source owns
            for target in sorted(declarations[service]):
                condition = declarations[service][target]
                key = (service, target)
                existing = collected.get(key)
                if existing is None:
                    collected[key] = (condition, [source])
                    continue
                combined = _merge_conditions(existing[0], condition)
                if combined is None:
                    problems.append(
                        GraphProblem(
                            "conflicting_conditions",
                            f"{service} declares two dependencies on {target} that cannot both "
                            "hold.",
                            (
                                f"{' and '.join(existing[1])} requires {existing[0]}; "
                                f"{source} requires {condition}.",
                                "One requires the service to be running and the other requires "
                                "it to have exited. Remove or correct one of them.",
                            ),
                        )
                    )
                    continue
                collected[key] = (combined, [*existing[1], source])

    dependencies: list[Dependency] = []
    for (service, target), (condition, sources) in sorted(collected.items()):
        if target not in known:
            problems.append(
                GraphProblem(
                    "undeclared_dependency",
                    f"{service} depends on {target!r}, which is not a declared service.",
                    (
                        f"Declared services: {', '.join(sorted(known)) or 'none'}.",
                        "Correct the dependency name or add the service.",
                    ),
                )
            )
            continue
        if (
            condition == "service_healthy"
            and healthchecked is not None
            and target not in healthchecked
        ):
            problems.append(
                GraphProblem(
                    "healthcheck_missing",
                    f"{service} waits for {target} to be healthy, but {target} has no Compose "
                    "health check.",
                    (
                        "That condition can never be met, so the start would wait until it "
                        "timed out.",
                        f"Add a healthcheck to {target} in the Compose file, or depend on "
                        "service_started, or declare a readiness probe for it in devdock.yaml "
                        "and depend on service_ready.",
                    ),
                )
            )
        # Kept even when its condition was just rejected: the edge itself is
        # real, so it must still take part in cycle detection. A user who adds
        # the missing health check should not then discover a cycle that was
        # visible all along. Only an edge whose target does not exist is
        # dropped, because there is no node to traverse to.
        dependencies.append(Dependency(service, target, condition, tuple(sources)))
    return tuple(dependencies), problems


def _successors(
    nodes: tuple[str, ...], dependencies: tuple[Dependency, ...]
) -> dict[str, tuple[str, ...]]:
    """Map each service to the services it depends on, in sorted order."""

    edges: dict[str, list[str]] = {name: [] for name in nodes}
    for item in dependencies:
        edges[item.service].append(item.depends_on)
    return {name: tuple(sorted(targets)) for name, targets in edges.items()}


def _cycle_problems(
    nodes: tuple[str, ...], dependencies: tuple[Dependency, ...]
) -> list[GraphProblem]:
    """One problem per cyclic group, each showing a concrete path through it.

    Enumerating every simple cycle can be exponential, so each strongly
    connected component is reported once with a single representative path. A
    user who breaks that path and runs again sees whatever remains.
    """

    edges = _successors(nodes, dependencies)
    problems: list[GraphProblem] = []
    for component in _strongly_connected_components(nodes, edges):
        members = set(component)
        if len(component) == 1:
            only = component[0]
            if only not in edges[only]:
                continue
        start = min(component)
        path = _cycle_through(start, members, edges)
        problems.append(
            GraphProblem(
                "cycle",
                f"Dependency cycle among {len(component)} service(s): no service in it can "
                "start first.",
                (
                    " depends on ".join(path),
                    f"Services involved: {', '.join(sorted(component))}.",
                    "Remove one of these dependencies to break the cycle.",
                ),
            )
        )
    return sorted(problems, key=lambda problem: problem.detail)


def _strongly_connected_components(
    nodes: tuple[str, ...], edges: Mapping[str, tuple[str, ...]]
) -> list[list[str]]:
    """Tarjan's algorithm, written iteratively.

    An explicit stack rather than recursion: a deeply chained Compose file must
    produce a refusal, not a ``RecursionError``.
    """

    order: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    pending: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for root in nodes:
        if root in order:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, position = work[-1]
            if position == 0:
                order[node] = low[node] = counter
                counter += 1
                pending.append(node)
                on_stack.add(node)
            successors = edges[node]
            descended = False
            while position < len(successors):
                successor = successors[position]
                position += 1
                if successor not in order:
                    work[-1] = (node, position)
                    work.append((successor, 0))
                    descended = True
                    break
                if successor in on_stack:
                    low[node] = min(low[node], order[successor])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == order[node]:
                component: list[str] = []
                while True:
                    member = pending.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)
    return components


def _cycle_through(
    start: str, members: set[str], edges: Mapping[str, tuple[str, ...]]
) -> tuple[str, ...]:
    """Find one path from ``start`` back to itself within ``members``."""

    def within(node: str) -> Iterator[str]:
        return iter(target for target in edges[node] if target in members)

    path = [start]
    visited = {start}
    stack: list[tuple[str, Iterator[str]]] = [(start, within(start))]
    while stack:
        _, successors = stack[-1]
        advanced = False
        for successor in successors:
            if successor == start:
                return (*path, start)
            if successor in visited:
                continue
            visited.add(successor)
            path.append(successor)
            stack.append((successor, within(successor)))
            advanced = True
            break
        if not advanced:
            stack.pop()
            path.pop()
    return (start, start)  # pragma: no cover - every component member reaches start


def _layers(
    nodes: tuple[str, ...], dependencies: tuple[Dependency, ...]
) -> tuple[tuple[str, ...], ...]:
    """Group services into the fewest layers that respect every dependency.

    Called only after cycle detection passed, so every node is placed.
    """

    edges = _successors(nodes, dependencies)
    remaining = dict(edges)
    placed: set[str] = set()
    layers: list[tuple[str, ...]] = []
    while remaining:
        ready = tuple(
            sorted(
                name
                for name, targets in remaining.items()
                if all(target in placed for target in targets)
            )
        )
        if not ready:  # pragma: no cover - unreachable once cycles are rejected
            raise ValueError("dependency graph still has a cycle after validation")
        layers.append(ready)
        placed.update(ready)
        for name in ready:
            del remaining[name]
    return tuple(layers)
