from pathlib import Path

from typer.testing import CliRunner

from devdock.cli import app

runner = CliRunner()


def test_init_inspect_and_check_work_as_a_minimal_path(tmp_path: Path) -> None:
    initialized = runner.invoke(app, ["init", str(tmp_path)])
    assert initialized.exit_code == 0
    assert (tmp_path / "devdock.yaml").is_file()

    inspected = runner.invoke(app, ["inspect", str(tmp_path)])
    assert inspected.exit_code == 0
    assert "Configured project:" in inspected.stdout

    checked = runner.invoke(app, ["check", str(tmp_path)])
    assert checked.exit_code == 0
    assert "[PASS] PROFILE_VALID: Profile is valid" in checked.stdout


def test_init_never_overwrites_an_existing_profile(tmp_path: Path) -> None:
    profile_path = tmp_path / "devdock.yaml"
    profile_path.write_text("user content\n", encoding="utf-8")

    result = runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 1
    assert profile_path.read_text(encoding="utf-8") == "user content\n"


def test_check_requires_a_profile(tmp_path: Path) -> None:
    result = runner.invoke(app, ["check", str(tmp_path)])

    assert result.exit_code == 1
    assert "devdock.yaml was not found" in result.stderr


def test_inspect_reports_invalid_profile(tmp_path: Path) -> None:
    (tmp_path / "devdock.yaml").write_text("schema_version: 99\n", encoding="utf-8")

    result = runner.invoke(app, ["inspect", str(tmp_path)])

    assert result.exit_code == 1
    assert "Profile error:" in result.stderr


def test_inspect_reports_discovery_evidence_and_known_files(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")

    result = runner.invoke(app, ["inspect", str(tmp_path)])

    assert result.exit_code == 0
    assert "Discovery evidence: compose.yaml" in result.stdout
    assert "Known project files: compose.yaml, pyproject.toml" in result.stdout


def test_inspect_explains_nested_roots_and_unicode_paths(tmp_path: Path) -> None:
    outer = tmp_path / "outer project"
    inner = outer / "café"
    inner.mkdir(parents=True)
    (outer / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (inner / "devdock.yaml").write_text(
        "schema_version: 1\nproject: nested\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["inspect", str(inner)])

    assert result.exit_code == 0
    assert str(inner.resolve()) in result.stdout
    assert "Nested project roots detected" in result.stdout


def test_init_reports_when_directory_cannot_be_created(tmp_path: Path) -> None:
    directory_is_a_file = tmp_path / "not-a-directory"
    directory_is_a_file.write_text("file\n", encoding="utf-8")

    result = runner.invoke(app, ["init", str(directory_is_a_file / "child")])

    assert result.exit_code == 1
    assert "Could not create project directory" in result.stderr

