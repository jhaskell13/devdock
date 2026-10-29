from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from devdock.config import ProfileError, ProjectProfile, load_profile


def test_loads_valid_profile(tmp_path: Path) -> None:
    profile_path = tmp_path / "devdock.yaml"
    profile_path.write_text(
        "schema_version: 1\nproject: sample\nruntimes: {}\nrequired_env: []\n",
        encoding="utf-8",
    )

    profile = load_profile(profile_path)

    assert profile.project == "sample"
    assert profile.runtimes == {}


def test_rejects_unknown_profile_fields(tmp_path: Path) -> None:
    profile_path = tmp_path / "devdock.yaml"
    profile_path.write_text(
        "schema_version: 1\nproject: sample\nunknown: value\n", encoding="utf-8"
    )

    with pytest.raises(ProfileError, match="Extra inputs are not permitted"):
        load_profile(profile_path)


def test_reports_malformed_yaml(tmp_path: Path) -> None:
    profile_path = tmp_path / "devdock.yaml"
    profile_path.write_text("project: [unterminated\n", encoding="utf-8")

    with pytest.raises(ProfileError):
        load_profile(profile_path)


@pytest.mark.parametrize(
    "compose_file",
    [
        Path("../outside.yaml"),
        Path("nested/../../outside.yaml"),
        Path("C:/private/compose.yaml"),
        Path("C:relative.yaml"),
        Path("invalid\0path.yaml"),
    ],
)
def test_rejects_compose_paths_outside_project(compose_file: Path) -> None:
    with pytest.raises(ValidationError, match="compose_file"):
        ProjectProfile.model_validate(
            {"schema_version": 1, "project": "sample", "compose_file": compose_file}
        )


def test_rejects_windows_separators_for_portable_paths() -> None:
    with pytest.raises(ValidationError, match="forward slashes"):
        ProjectProfile.model_validate(
            {"schema_version": 1, "project": "sample", "compose_file": r"sub\compose.yaml"}
        )


@pytest.mark.parametrize("path", ["../outside", "C:/outside", r"nested\file"])
def test_rejects_unsafe_required_paths(path: str) -> None:
    with pytest.raises(ValidationError, match="required paths"):
        ProjectProfile.model_validate(
            {"schema_version": 1, "project": "sample", "required_files": [path]}
        )


@pytest.mark.parametrize("port", [0, 65536, True, "8080"])
def test_rejects_invalid_ports(port: object) -> None:
    with pytest.raises(ValidationError):
        ProjectProfile.model_validate({"schema_version": 1, "project": "sample", "ports": [port]})


def test_rejects_duplicate_ports() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        ProjectProfile.model_validate(
            {"schema_version": 1, "project": "sample", "ports": [8080, 8080]}
        )


@pytest.mark.parametrize("field", ["compose_file", "required_files", "required_dirs"])
@pytest.mark.parametrize("hostile", ["a\tb.yaml", "a\nb.yaml", "a\x1b[2Jb.yaml", "a\x7fb"])
def test_paths_with_control_characters_are_rejected(
    tmp_path: Path, field: str, hostile: str
) -> None:
    value = hostile if field == "compose_file" else [hostile]
    profile_path = tmp_path / "devdock.yaml"
    profile_path.write_text(
        yaml.safe_dump({"schema_version": 1, "project": "sample", field: value}),
        encoding="utf-8",
    )

    with pytest.raises(ProfileError, match="control characters|project-contained"):
        load_profile(profile_path)
