"""Ownership scope: which Compose resources DevDock may act on, and proof of it.

DevDock never adopts resources because a name looks familiar. It derives a
Compose project name that only DevDock would produce, then requires every
resource under that name to carry the labels Compose would have written when
DevDock itself ran ``docker compose`` from this project root with this Compose
file. Anything else under that name is a conflict, and lifecycle commands must
refuse to act until the user resolves it.

The rules in this module are pure functions over data the adapter collected.
They never run commands or touch the filesystem beyond resolving the root path.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from devdock.adapters.base import ContainerRecord, NetworkRecord, ResourceInventory
from devdock.config import ProjectProfile
from devdock.discovery import COMPOSE_NAMES

NAME_PREFIX = "devdock"
ROOT_DIGEST_LENGTH = 12
SLUG_MAX_LENGTH = 40
_VALID_PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SLUG_SEPARATORS = re.compile(r"[^a-z0-9]+")

PROJECT_LABEL = "com.docker.compose.project"
WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"
SERVICE_LABEL = "com.docker.compose.service"
ONEOFF_LABEL = "com.docker.compose.oneoff"

ConflictKind = Literal["mismatched", "oneoff", "unlabeled", "unparseable", "inconsistent"]


def compose_path_for(profile: ProjectProfile, root: Path) -> Path | None:
    """Return the configured Compose file, else the first conventional one present."""

    if profile.compose_file is not None:
        return root / profile.compose_file
    return next((root / name for name in COMPOSE_NAMES if (root / name).is_file()), None)


def resolves_inside(path: Path, root: Path) -> bool:
    """Whether ``path`` stays within ``root`` after symlinks are resolved."""

    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def _slug(project: str) -> str:
    slug = _SLUG_SEPARATORS.sub("-", project.lower()).strip("-")
    slug = slug[:SLUG_MAX_LENGTH].strip("-")
    return slug or "project"


def _root_digest(root: Path) -> str:
    canonical = os.path.normcase(str(root.resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:ROOT_DIGEST_LENGTH]


def derive_project_name(project: str, root: Path) -> str:
    """Return the Compose project name DevDock uses for ``project`` at ``root``.

    The name is stable for one project at one resolved directory, differs for
    the same project copied elsewhere, and never equals a name a person would
    choose by hand without copying the prefix and digest.
    """

    name = f"{NAME_PREFIX}-{_slug(project)}-{_root_digest(root)}"
    if not _VALID_PROJECT_NAME.fullmatch(name):  # pragma: no cover - defensive
        raise ValueError(f"derived project name is not Compose-safe: {name!r}")
    return name


def _normalize_path_text(text: str) -> str:
    return os.path.normcase(os.path.normpath(text))


@dataclass(frozen=True)
class OwnershipScope:
    """Everything DevDock must see on a resource before treating it as its own."""

    project_name: str
    root: Path
    compose_file: Path | None

    @property
    def expected_working_dir(self) -> str:
        return str(self.root)

    @property
    def expected_config_files(self) -> str | None:
        return None if self.compose_file is None else str(self.compose_file)

    def describe(self) -> tuple[str, ...]:
        """Human-readable lines printed before any lifecycle action."""

        compose = "none configured or discovered" if self.compose_file is None else str(
            self.compose_file
        )
        return (
            f"Compose project name: {self.project_name}",
            f"Project root: {self.root}",
            f"Compose file: {compose}",
            "DevDock acts only on resources that Compose labeled with this project name, "
            "working directory, and Compose file.",
        )


def ownership_scope(profile: ProjectProfile, root: Path) -> OwnershipScope:
    """Resolve the scope for ``profile`` at ``root`` without touching Docker."""

    resolved_root = root.resolve()
    compose_path = compose_path_for(profile, resolved_root)
    return OwnershipScope(
        project_name=derive_project_name(profile.project, resolved_root),
        root=resolved_root,
        compose_file=compose_path,
    )


@dataclass(frozen=True)
class Conflict:
    """A resource under DevDock's project name that DevDock cannot prove it created."""

    kind: ConflictKind
    name: str
    reasons: tuple[str, ...]


def classify_container(record: ContainerRecord, scope: OwnershipScope) -> Conflict | None:
    """Return ``None`` when DevDock would have created ``record``; else the conflict."""

    if record.parse_error:
        return Conflict("unparseable", record.name or record.id, (record.parse_error,))
    if record.project != scope.project_name:
        return Conflict(
            "unlabeled",
            record.name,
            (f"Missing or different {PROJECT_LABEL} label.",),
        )
    if record.oneoff.lower() == "true":
        return Conflict(
            "oneoff",
            record.name,
            ("Created by 'docker compose run', not by a DevDock service start.",),
        )

    reasons: list[str] = []
    if not record.service:
        reasons.append(f"Missing {SERVICE_LABEL} label.")
    if _normalize_path_text(record.working_dir) != _normalize_path_text(
        scope.expected_working_dir
    ):
        reasons.append(
            f"Working directory label is {record.working_dir or '(empty)'}; "
            f"expected {scope.expected_working_dir}."
        )
    expected_config = scope.expected_config_files
    actual_files = tuple(part for part in record.config_files.split(",") if part)
    if expected_config is None:
        reasons.append("No Compose file is configured, so DevDock cannot match config_files.")
    elif len(actual_files) != 1 or _normalize_path_text(actual_files[0]) != _normalize_path_text(
        expected_config
    ):
        reasons.append(
            f"Compose file label is {record.config_files or '(empty)'}; "
            f"expected exactly {expected_config}."
        )
    if reasons:
        return Conflict("mismatched", record.name, tuple(reasons))
    return None


@dataclass(frozen=True)
class OwnershipAssessment:
    """Result of comparing the live inventory with the expected scope."""

    scope: OwnershipScope
    managed: tuple[ContainerRecord, ...]
    conflicts: tuple[Conflict, ...]
    networks: tuple[NetworkRecord, ...]
    volumes: tuple[str, ...]
    errors: tuple[str, ...] = field(default=())

    @property
    def safe_to_act(self) -> bool:
        """True only when every resource under the name is provably DevDock's."""

        return not self.conflicts and not self.errors

    def explain(self) -> tuple[str, ...]:
        """Lines a lifecycle command prints as its ownership preview."""

        lines = list(self.scope.describe())
        if self.managed:
            lines.append(f"Managed containers ({len(self.managed)}):")
            lines.extend(
                f"  {item.name} [{item.service}] {item.state}" for item in self.managed
            )
        else:
            lines.append("Managed containers: none")
        if self.networks:
            lines.append(f"Project networks ({len(self.networks)}):")
            lines.extend(f"  {item.name}" for item in self.networks)
        if self.volumes:
            lines.append(
                f"Project volumes ({len(self.volumes)}), preserved; "
                "DevDock does not remove volumes in this version:"
            )
            lines.extend(f"  {name}" for name in self.volumes)
        for error in self.errors:
            lines.append(f"Inventory incomplete: {error}")
        if self.conflicts:
            lines.append(
                f"Refusing to act: {len(self.conflicts)} resource(s) under this project name "
                "were not created by DevDock from this project."
            )
            for conflict in self.conflicts:
                lines.append(f"  {conflict.name} ({conflict.kind})")
                lines.extend(f"    {reason}" for reason in conflict.reasons)
            lines.append(
                "Inspect them with: docker ps -a --filter "
                f"label={PROJECT_LABEL}={self.scope.project_name}"
            )
            lines.append(
                "If they belong to an earlier configuration of this project, stop them with "
                "the configuration that created them, then retry."
            )
        elif self.errors:
            lines.append(
                "Refusing to act: DevDock could not confirm which resources it owns."
            )
        return tuple(lines)


def assess_ownership(scope: OwnershipScope, inventory: ResourceInventory) -> OwnershipAssessment:
    """Classify every inventoried resource against ``scope``."""

    managed: list[ContainerRecord] = []
    conflicts: list[Conflict] = []
    seen: set[str] = set()
    for record in inventory.containers:
        seen.add(record.id)
        conflict = classify_container(record, scope)
        if conflict is None:
            managed.append(record)
        else:
            conflicts.append(conflict)
    for record in inventory.name_collisions:
        if record.id in seen:
            continue
        seen.add(record.id)
        conflict = classify_container(record, scope)
        if conflict is None:
            # Reaching here means the label query missed a labeled container.
            conflict = Conflict(
                "inconsistent",
                record.name,
                ("Container matched by name but not by the project label query.",),
            )
        conflicts.append(conflict)
    return OwnershipAssessment(
        scope=scope,
        managed=tuple(managed),
        conflicts=tuple(conflicts),
        networks=inventory.networks,
        volumes=inventory.volumes,
        errors=inventory.errors,
    )
