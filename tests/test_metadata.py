from pathlib import Path

import pytest

from devdock.metadata import read_python_metadata


def test_reads_python_declarations_without_reading_env_templates(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nrequires-python = ">=3.12"\n', encoding="utf-8"
    )
    (tmp_path / ".python-version").write_text("3.12.4\n", encoding="utf-8")
    (tmp_path / ".tool-versions").write_text("node 22.0.0\npython 3.13.1\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("TOKEN=not-for-reading\n", encoding="utf-8")

    metadata = read_python_metadata(tmp_path)

    assert [(item.source, item.value) for item in metadata.declarations] == [
        ("pyproject.toml: project.requires-python", ">=3.12"),
        (".python-version", "3.12.4"),
        (".tool-versions", "3.13.1"),
    ]
    assert not metadata.errors
    assert "not-for-reading" not in repr(metadata)


def test_reports_malformed_python_metadata_without_exposing_file_contents(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        'secret = "must-not-be-reported"\nproject = [broken\n', encoding="utf-8"
    )

    metadata = read_python_metadata(tmp_path)

    assert metadata.errors
    assert "must-not-be-reported" not in repr(metadata)


def test_does_not_follow_metadata_symlink_outside_project(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-metadata"
    outside.mkdir()
    (outside / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.99"\n', encoding="utf-8"
    )
    try:
        (tmp_path / "pyproject.toml").symlink_to(outside / "pyproject.toml")
    except OSError as exc:
        pytest.skip(f"Creating file symlinks is not permitted here: {exc}")

    metadata = read_python_metadata(tmp_path)

    assert metadata.declarations == ()
    assert metadata.errors == ()
