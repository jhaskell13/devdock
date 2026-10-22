from pathlib import Path

from devdock.discovery import discover_project, discover_project_result


def test_selects_nearest_matching_ancestor(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

    project = outer / "project"
    project.mkdir()
    (project / "devdock.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    nested = project / "src" / "package"
    nested.mkdir(parents=True)

    assert discover_project(nested) == project.resolve()


def test_uses_nearest_manifest_ancestor_when_start_has_no_marker(tmp_path: Path) -> None:
    nested = tmp_path / "unmarked" / "folder"
    nested.mkdir(parents=True)

    assert discover_project(nested) == Path(__file__).parents[1].resolve()


def test_result_exposes_selected_evidence_inventory_and_nested_roots(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (inner / "devdock.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (inner / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (inner / ".env").write_text("SECRET=never-read\n", encoding="utf-8")

    result = discover_project_result(inner)

    assert result.root == inner.resolve()
    assert result.matched_markers == ("devdock.yaml", "pyproject.toml")
    assert outer.resolve() in result.ancestor_roots
    assert Path(__file__).parents[1].resolve() in result.ancestor_roots
    assert result.ambiguous
    assert "pyproject.toml" in result.known_files
    assert ".env" not in result.known_files


def test_python_project_metadata_can_select_a_root_without_devDock_or_compose(
    tmp_path: Path,
) -> None:
    root = tmp_path / "python-project"
    nested = root / "src" / "package"
    nested.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    result = discover_project_result(nested)

    assert result.root == root.resolve()
    assert result.matched_markers == ("pyproject.toml",)


def test_discovery_handles_unicode_and_spaces_in_project_paths(tmp_path: Path) -> None:
    root = tmp_path / "café project"
    nested = root / "src folder"
    nested.mkdir(parents=True)
    (root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

    result = discover_project_result(nested)

    assert result.root == root.resolve()
    assert result.matched_markers == ("compose.yaml",)

