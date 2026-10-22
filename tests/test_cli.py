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
    assert "[PASS] Profile is valid" in checked.stdout

