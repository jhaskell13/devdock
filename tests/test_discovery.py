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


def test_uses_start_directory_when_no_marker_exists(tmp_path: Path) -> None:
    nested = tmp_path / "unmarked" / "folder"
    nested.mkdir(parents=True)

    assert discover_project(nested) == nested.resolve()


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
    assert result.matched_markers == ("devdock.yaml",)
    assert result.ancestor_roots == (outer.resolve(),)
    assert result.ambiguous
    assert "pyproject.toml" in result.known_files
    assert ".env" not in result.known_files

