from pathlib import Path

from devdock.discovery import discover_project


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

