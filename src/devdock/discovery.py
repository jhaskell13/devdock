"""Deterministic project-root discovery and safe file inventory."""

from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "devdock.yaml"
COMPOSE_NAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)
PROJECT_FILES = (
    *COMPOSE_NAMES,
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "poetry.lock",
    "uv.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "environment.yml",
    "conda.yaml",
    ".python-version",
    ".tool-versions",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "go.mod",
    ".env.example",
    ".env.template",
)
ROOT_MARKERS = (
    CONFIG_NAME,
    *COMPOSE_NAMES,
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
)


@dataclass(frozen=True)
class DiscoveryResult:
    """Selected project root and the filesystem evidence supporting it."""

    start: Path
    root: Path
    matched_markers: tuple[str, ...]
    ancestor_roots: tuple[Path, ...]
    known_files: tuple[str, ...]

    @property
    def ambiguous(self) -> bool:
        """Whether another project marker exists above the selected root."""

        return bool(self.ancestor_roots)


def discover_project_result(start: Path) -> DiscoveryResult:
    """Discover the nearest root and report markers, ancestors, and known files."""

    resolved_start = start.resolve()
    candidates = (resolved_start, *resolved_start.parents)
    root = resolved_start
    matched: tuple[str, ...] = ()
    for candidate in candidates:
        markers = tuple(
            name
            for name in ROOT_MARKERS
            if (candidate / name).is_file()
        )
        if markers:
            root, matched = candidate, markers
            break

    ancestors = tuple(
        candidate
        for candidate in candidates
        if candidate != root
        and len(candidate.parts) < len(root.parts)
        and any((candidate / name).is_file() for name in ROOT_MARKERS)
    )
    files = tuple(name for name in PROJECT_FILES if (root / name).is_file())
    return DiscoveryResult(resolved_start, root, matched, ancestors, files)


def discover_project(start: Path) -> Path:
    """Find the nearest parent containing a DevDock profile or Compose file."""

    return discover_project_result(start).root

