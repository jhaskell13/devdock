import os
import re
from pathlib import Path

import pytest

from devdock.adapters import ContainerRecord, NetworkRecord, ResourceInventory
from devdock.config import ProjectProfile
from devdock.ownership import (
    PROJECT_LABEL,
    OwnershipScope,
    assess_ownership,
    classify_container,
    derive_project_name,
    ownership_scope,
)

COMPOSE_SAFE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def make_scope(tmp_path: Path, compose: str | None = "compose.yaml") -> OwnershipScope:
    root = tmp_path.resolve()
    return OwnershipScope(
        project_name=derive_project_name("sample", root),
        root=root,
        compose_file=None if compose is None else root / compose,
    )


def managed_record(scope: OwnershipScope, **overrides: str) -> ContainerRecord:
    fields = dict(
        id="abc123",
        name=f"{scope.project_name}-web-1",
        state="running",
        project=scope.project_name,
        working_dir=str(scope.root),
        config_files=str(scope.compose_file),
        service="web",
        oneoff="False",
    )
    fields.update(overrides)
    return ContainerRecord(**fields)  # type: ignore[arg-type]


# --- name derivation -------------------------------------------------------


def test_project_name_is_deterministic_and_compose_safe(tmp_path: Path) -> None:
    first = derive_project_name("Sample Project", tmp_path)
    second = derive_project_name("Sample Project", tmp_path)

    assert first == second
    assert first.startswith("devdock-sample-project-")
    assert COMPOSE_SAFE.fullmatch(first)
    assert len(first.rsplit("-", 1)[1]) == 12


def test_project_name_differs_by_root_and_by_project(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    assert derive_project_name("sample", a) != derive_project_name("sample", b)
    assert derive_project_name("sample", a) != derive_project_name("other", a)


def test_symlinked_root_yields_the_same_name(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks not permitted: {exc}")

    assert derive_project_name("sample", real) == derive_project_name("sample", link)


@pytest.mark.parametrize(
    ("project", "expected_slug"),
    [
        ("café au lait", "caf-au-lait"),
        ("  --weird__NAME!!  ", "weird-name"),
        ("---", "project"),
        ("日本語", "project"),
        ("a" * 80, "a" * 40),
        ("UPPER", "upper"),
    ],
)
def test_slug_normalization_never_produces_unsafe_names(
    tmp_path: Path, project: str, expected_slug: str
) -> None:
    name = derive_project_name(project, tmp_path)

    assert COMPOSE_SAFE.fullmatch(name)
    assert name.split("-", 1)[1].rsplit("-", 1)[0] == expected_slug


def test_scope_resolves_root_and_prefers_configured_compose_file(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "custom.yaml").write_text("services: {}\n", encoding="utf-8")
    configured = ProjectProfile(
        schema_version=1, project="sample", compose_file=Path("custom.yaml")
    )
    discovered = ProjectProfile(schema_version=1, project="sample")
    none = ProjectProfile(schema_version=1, project="sample")
    empty = tmp_path / "empty"
    empty.mkdir()

    assert ownership_scope(configured, tmp_path).compose_file == tmp_path.resolve() / "custom.yaml"
    assert ownership_scope(discovered, tmp_path).compose_file == (
        tmp_path.resolve() / "compose.yaml"
    )
    assert ownership_scope(none, empty).compose_file is None
    assert ownership_scope(none, empty).root == empty.resolve()


def test_scope_description_names_the_boundary(tmp_path: Path) -> None:
    lines = make_scope(tmp_path).describe()

    assert lines[0].startswith("Compose project name: devdock-sample-")
    assert any("acts only on resources" in line for line in lines)


# --- container classification ---------------------------------------------


def test_container_with_exact_labels_is_managed(tmp_path: Path) -> None:
    scope = make_scope(tmp_path)

    assert classify_container(managed_record(scope), scope) is None


def test_trailing_separators_and_case_do_not_defeat_path_match(tmp_path: Path) -> None:
    scope = make_scope(tmp_path)
    record = managed_record(scope, working_dir=str(scope.root) + os.sep)

    assert classify_container(record, scope) is None


def test_different_working_directory_is_a_mismatch(tmp_path: Path) -> None:
    scope = make_scope(tmp_path)
    record = managed_record(scope, working_dir=str(tmp_path.parent / "elsewhere"))

    conflict = classify_container(record, scope)

    assert conflict is not None and conflict.kind == "mismatched"
    assert any("Working directory" in reason for reason in conflict.reasons)


def test_different_or_multiple_compose_files_are_a_mismatch(tmp_path: Path) -> None:
    scope = make_scope(tmp_path)
    other = managed_record(scope, config_files=str(scope.root / "other.yaml"))
    multiple = managed_record(
        scope, config_files=f"{scope.compose_file},{scope.root / 'override.yaml'}"
    )

    for record in (other, multiple):
        conflict = classify_container(record, scope)
        assert conflict is not None and conflict.kind == "mismatched"
        assert any("Compose file label" in reason for reason in conflict.reasons)


def test_missing_service_label_is_a_mismatch(tmp_path: Path) -> None:
    scope = make_scope(tmp_path)

    conflict = classify_container(managed_record(scope, service=""), scope)

    assert conflict is not None and conflict.kind == "mismatched"


def test_oneoff_container_is_never_managed(tmp_path: Path) -> None:
    scope = make_scope(tmp_path)

    conflict = classify_container(managed_record(scope, oneoff="True"), scope)

    assert conflict is not None and conflict.kind == "oneoff"


def test_container_without_project_label_is_unlabeled(tmp_path: Path) -> None:
    scope = make_scope(tmp_path)
    record = ContainerRecord(id="x", name=f"{scope.project_name}-web-1", state="running")

    conflict = classify_container(record, scope)

    assert conflict is not None and conflict.kind == "unlabeled"
    assert PROJECT_LABEL in conflict.reasons[0]


def test_unparseable_record_is_a_conflict(tmp_path: Path) -> None:
    scope = make_scope(tmp_path)
    record = ContainerRecord(id="", name="", state="", parse_error="Expected 8 fields, got 3.")

    conflict = classify_container(record, scope)

    assert conflict is not None and conflict.kind == "unparseable"


def test_scope_without_compose_file_cannot_claim_anything(tmp_path: Path) -> None:
    scope = make_scope(tmp_path, compose=None)
    record = managed_record(scope, config_files=str(tmp_path / "compose.yaml"))

    conflict = classify_container(record, scope)

    assert conflict is not None and conflict.kind == "mismatched"


# --- assessment -----------------------------------------------------------


def test_empty_inventory_is_safe(tmp_path: Path) -> None:
    scope = make_scope(tmp_path)

    assessment = assess_ownership(scope, ResourceInventory(scope.project_name))

    assert assessment.safe_to_act
    assert "Managed containers: none" in assessment.explain()


def test_only_managed_resources_is_safe_and_lists_volumes_as_preserved(
    tmp_path: Path,
) -> None:
    scope = make_scope(tmp_path)
    inventory = ResourceInventory(
        scope.project_name,
        containers=(managed_record(scope), managed_record(scope, id="def", service="db")),
        networks=(NetworkRecord("n1", f"{scope.project_name}_default"),),
        volumes=(f"{scope.project_name}_data",),
    )

    assessment = assess_ownership(scope, inventory)
    text = "\n".join(assessment.explain())

    assert assessment.safe_to_act
    assert len(assessment.managed) == 2
    assert "preserved" in text and f"{scope.project_name}_data" in text
    assert "Refusing" not in text


def test_any_conflict_blocks_action_and_explains_how_to_inspect(tmp_path: Path) -> None:
    scope = make_scope(tmp_path)
    inventory = ResourceInventory(
        scope.project_name,
        containers=(
            managed_record(scope),
            managed_record(scope, id="zzz", name="intruder", working_dir="/somewhere/else"),
        ),
    )

    assessment = assess_ownership(scope, inventory)
    text = "\n".join(assessment.explain())

    assert not assessment.safe_to_act
    assert len(assessment.managed) == 1 and len(assessment.conflicts) == 1
    assert "Refusing to act" in text
    assert "intruder (mismatched)" in text
    assert f"docker ps -a --filter label={PROJECT_LABEL}={scope.project_name}" in text


def test_name_collision_without_label_blocks_action(tmp_path: Path) -> None:
    scope = make_scope(tmp_path)
    squatter = ContainerRecord(id="q1", name=f"{scope.project_name}-web-1", state="exited")
    inventory = ResourceInventory(scope.project_name, name_collisions=(squatter,))

    assessment = assess_ownership(scope, inventory)

    assert not assessment.safe_to_act
    assert assessment.conflicts[0].kind == "unlabeled"


def test_name_collision_duplicates_of_labeled_containers_are_not_double_counted(
    tmp_path: Path,
) -> None:
    scope = make_scope(tmp_path)
    record = managed_record(scope)
    inventory = ResourceInventory(
        scope.project_name, containers=(record,), name_collisions=(record,)
    )

    assessment = assess_ownership(scope, inventory)

    assert assessment.safe_to_act and len(assessment.managed) == 1


def test_inventory_errors_block_action_even_with_no_conflicts(tmp_path: Path) -> None:
    scope = make_scope(tmp_path)
    inventory = ResourceInventory(
        scope.project_name, errors=("Network query failed with exit code 1.",)
    )

    assessment = assess_ownership(scope, inventory)
    text = "\n".join(assessment.explain())

    assert not assessment.safe_to_act
    assert "Inventory incomplete" in text and "could not confirm" in text


def test_container_model_holds_only_identity_and_ownership_fields() -> None:
    # Nothing else about a container (image, env, full label set) exists in the
    # model, so the explanation cannot leak it.
    assert set(ContainerRecord.__dataclass_fields__) == {
        "id", "name", "state", "project", "working_dir", "config_files",
        "service", "oneoff", "parse_error",
    }


def test_name_match_without_label_query_hit_is_reported_as_inconsistent(
    tmp_path: Path,
) -> None:
    scope = make_scope(tmp_path)
    labeled_but_missed = managed_record(scope)
    inventory = ResourceInventory(scope.project_name, name_collisions=(labeled_but_missed,))

    assessment = assess_ownership(scope, inventory)

    assert not assessment.safe_to_act
    assert assessment.conflicts[0].kind == "inconsistent"
