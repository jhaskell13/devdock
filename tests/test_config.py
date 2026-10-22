from pathlib import Path

import pytest

from devdock.config import ProfileError, load_profile


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

