from pathlib import Path

from devdock.config import ProjectProfile
from devdock.diagnostics import check_profile


def test_unsupported_runtime_is_actionable(tmp_path: Path) -> None:
    profile = ProjectProfile(
        schema_version=1,
        project="sample",
        runtimes={"ruby": ">=3.2"},
    )

    findings = check_profile(profile, tmp_path)

    assert any(finding.level == "FAIL" and "ruby" in finding.message for finding in findings)


def test_missing_compose_file_is_reported(tmp_path: Path) -> None:
    profile = ProjectProfile(
        schema_version=1,
        project="sample",
        compose_file=Path("compose.yaml"),
    )

    findings = check_profile(profile, tmp_path)

    assert any(finding.level == "FAIL" and "Compose file not found" in finding.message for finding in findings)

