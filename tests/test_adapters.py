import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from devdock.adapters import (
    CommandResult,
    ComposeAdapter,
    NetworkRecord,
    PublishedPort,
    ServiceObservation,
    run_command,
)
from devdock.config import ProjectProfile
from devdock.diagnostics import check_profile


class RecordingRunner:
    """Fake CommandRunner returning scripted results and recording argument arrays."""

    def __init__(self, results: list[CommandResult | BaseException]) -> None:
        self._results = iter(results)
        self.calls: list[tuple[list[str], Path | None, float]] = []

    def __call__(
        self, args: Sequence[str], *, cwd: Path | None = None, timeout: float
    ) -> CommandResult:
        self.calls.append((list(args), cwd, timeout))
        result = next(self._results)
        if isinstance(result, BaseException):
            raise result
        return result


OK = CommandResult(0, "", "")


def test_missing_docker_cli_short_circuits_without_running_commands() -> None:
    runner = RecordingRunner([])
    adapter = ComposeAdapter(runner, locate=lambda _: None)

    capabilities = adapter.detect_capabilities()

    assert capabilities.executable is None
    assert capabilities.compose_cli == "missing"
    assert capabilities.daemon == "not_checked"
    assert runner.calls == []


def test_compose_plugin_failure_prevents_daemon_check() -> None:
    runner = RecordingRunner([CommandResult(1, "", "plugin missing")])
    adapter = ComposeAdapter(runner, locate=lambda _: "/usr/bin/docker")

    capabilities = adapter.detect_capabilities()

    assert capabilities.compose_cli == "failed"
    assert capabilities.compose_cli_returncode == 1
    assert capabilities.daemon == "not_checked"
    assert [call[0] for call in runner.calls] == [["/usr/bin/docker", "compose", "version"]]


@pytest.mark.parametrize(
    "failure", [OSError("cannot start"), subprocess.TimeoutExpired(["docker"], 5)]
)
def test_unresponsive_compose_plugin_is_distinguished_from_failed(
    failure: BaseException,
) -> None:
    adapter = ComposeAdapter(RecordingRunner([failure]), locate=lambda _: "docker")

    assert adapter.detect_capabilities().compose_cli == "unresponsive"


def test_capabilities_use_argument_arrays_and_bounded_timeouts() -> None:
    runner = RecordingRunner([OK, OK])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    capabilities = adapter.detect_capabilities()

    assert capabilities.compose_available and capabilities.daemon_available
    assert [call[0] for call in runner.calls] == [
        ["docker", "compose", "version"],
        ["docker", "info", "--format", "{{.ServerVersion}}"],
    ]
    assert all(call[2] > 0 for call in runner.calls)


def test_daemon_timeout_reports_unavailable_but_keeps_compose_available() -> None:
    runner = RecordingRunner([OK, subprocess.TimeoutExpired(["docker"], 5)])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    capabilities = adapter.detect_capabilities()

    assert capabilities.compose_available
    assert capabilities.daemon == "unavailable"


def test_validate_config_runs_from_project_root_and_hides_output(tmp_path: Path) -> None:
    runner = RecordingRunner([CommandResult(1, "", "TOKEN=secret-value")])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")
    compose_path = tmp_path / "compose.yaml"

    validation = adapter.validate_config(compose_path, project_root=tmp_path)

    assert validation.outcome == "invalid"
    assert validation.returncode == 1
    assert "secret-value" not in repr(validation)
    args, cwd, _timeout = runner.calls[0]
    assert args == ["docker", "compose", "-f", str(compose_path), "config", "--quiet"]
    assert cwd == tmp_path


def test_validate_config_without_docker_or_on_timeout_is_unavailable(tmp_path: Path) -> None:
    missing = ComposeAdapter(RecordingRunner([]), locate=lambda _: None)
    timing_out = ComposeAdapter(
        RecordingRunner([subprocess.TimeoutExpired(["docker"], 10)]), locate=lambda _: "docker"
    )
    path = tmp_path / "compose.yaml"

    assert missing.validate_config(path, project_root=tmp_path).outcome == "unavailable"
    assert timing_out.validate_config(path, project_root=tmp_path).outcome == "unavailable"


def test_check_profile_accepts_an_injected_adapter(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    profile = ProjectProfile(
        schema_version=1,
        project="sample",
        runtimes={"docker": "compose"},
        compose_file=Path("compose.yaml"),
    )
    runner = RecordingRunner([OK, OK, OK])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    findings = check_profile(profile, tmp_path, adapter=adapter)
    codes = {item.code for item in findings}

    assert {"COMPOSE_CLI_AVAILABLE", "DOCKER_DAEMON_AVAILABLE", "COMPOSE_CONFIG_VALID"} <= codes
    assert len(runner.calls) == 3


def test_default_runner_never_uses_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "out", "err")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_command(("docker", "compose", "version"), timeout=1.5)

    assert result == CommandResult(0, "out", "err")
    assert captured["args"] == ["docker", "compose", "version"]
    assert captured.get("shell") in (None, False)
    assert captured["timeout"] == 1.5
    assert captured["capture_output"] is True and captured["check"] is False


# --- resource inventory ---------------------------------------------------


def container_line(*fields: str) -> str:
    return "\t".join(fields)


def test_inventory_queries_use_label_and_anchored_name_filters() -> None:
    runner = RecordingRunner([OK, OK, OK, OK])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    inventory = adapter.list_project_resources("devdock-sample-0123456789ab")

    assert inventory.errors == ()
    argv = [call[0] for call in runner.calls]
    label = "label=com.docker.compose.project=devdock-sample-0123456789ab"
    assert argv[0][:5] == ["docker", "ps", "--all", "--no-trunc", "--filter"]
    assert argv[0][5] == label
    assert argv[1][5] == "name=^devdock-sample-0123456789ab-"
    assert argv[2][:5] == ["docker", "network", "ls", "--filter", label]
    assert argv[3][:5] == ["docker", "volume", "ls", "--filter", label]
    for args in argv:
        fmt = args[args.index("--format") + 1]
        assert "{{json" not in fmt and "inspect" not in args
        assert ".Env" not in fmt


def test_inventory_rejects_unsafe_project_names_before_running_anything() -> None:
    runner = RecordingRunner([])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    for bad in ("Has Space", "semi;colon", "", "-leading", "a|b"):
        with pytest.raises(ValueError):
            adapter.list_project_resources(bad)
    assert runner.calls == []


def test_inventory_parses_container_fields_and_flags_malformed_lines() -> None:
    good = container_line(
        "id1",
        "devdock-s-0123456789ab-web-1",
        "running",
        "devdock-s-0123456789ab",
        "/proj",
        "/proj/compose.yaml",
        "web",
        "False",
    )
    bad = container_line("id2", "broken", "running")
    runner = RecordingRunner(
        [
            CommandResult(0, f"{good}\n{bad}\n\n", ""),
            OK,
            CommandResult(0, "n1\tdevdock-s-0123456789ab_default\nmalformed\n", ""),
            CommandResult(0, "devdock-s-0123456789ab_data\n", ""),
        ]
    )
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    inventory = adapter.list_project_resources("devdock-s-0123456789ab")

    first, second = inventory.containers
    assert first.id == "id1" and first.service == "web" and first.parse_error is None
    assert first.config_files == "/proj/compose.yaml"
    assert second.parse_error is not None and second.name == "broken"
    assert inventory.networks == (NetworkRecord("n1", "devdock-s-0123456789ab_default"),)
    assert inventory.volumes == ("devdock-s-0123456789ab_data",)


def test_inventory_records_failed_queries_as_errors_without_output() -> None:
    runner = RecordingRunner(
        [
            CommandResult(1, "", "permission denied: SECRET=1"),
            subprocess.TimeoutExpired(["docker"], 10),
            OK,
            OK,
        ]
    )
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    inventory = adapter.list_project_resources("devdock-s-0123456789ab")

    assert len(inventory.errors) == 2
    assert "exit code 1" in inventory.errors[0]
    assert "did not complete" in inventory.errors[1]
    assert "SECRET" not in repr(inventory)


def test_inventory_without_docker_cli_reports_an_error() -> None:
    adapter = ComposeAdapter(RecordingRunner([]), locate=lambda _: None)

    inventory = adapter.list_project_resources("devdock-s-0123456789ab")

    assert inventory.errors == ("Docker CLI is not available.",)
    assert inventory.containers == ()


# --- start ----------------------------------------------------------------


def test_start_uses_devdock_controlled_options_only(tmp_path: Path) -> None:
    runner = RecordingRunner([CommandResult(0, "Container web started", "")])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")
    compose_file = tmp_path / "compose.yaml"

    outcome = adapter.start(
        project_name="devdock-s-0123456789ab",
        project_root=tmp_path,
        compose_file=compose_file,
        timeout=30,
    )

    assert outcome.succeeded and outcome.returncode == 0
    args, cwd, timeout = runner.calls[0]
    assert args == [
        "docker",
        "compose",
        "--project-directory",
        str(tmp_path),
        "--file",
        str(compose_file),
        "--project-name",
        "devdock-s-0123456789ab",
        "up",
        "--detach",
    ]
    assert cwd == tmp_path and timeout == 30


def test_start_never_waits_for_readiness_or_removes_orphans(tmp_path: Path) -> None:
    runner = RecordingRunner([OK])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    adapter.start(
        project_name="devdock-s-0123456789ab",
        project_root=tmp_path,
        compose_file=tmp_path / "compose.yaml",
        timeout=30,
    )

    args = runner.calls[0][0]
    assert "--wait" not in args
    assert "--remove-orphans" not in args
    assert "--force-recreate" not in args


def test_start_reports_failure_with_output_for_the_caller_to_redact(tmp_path: Path) -> None:
    runner = RecordingRunner([CommandResult(1, "", "port is already allocated")])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    outcome = adapter.start(
        project_name="devdock-s-0123456789ab",
        project_root=tmp_path,
        compose_file=tmp_path / "compose.yaml",
        timeout=30,
    )

    assert outcome.status == "failed" and outcome.returncode == 1
    assert outcome.raw_stderr == "port is already allocated"


def test_start_timeout_keeps_partial_output_including_bytes(tmp_path: Path) -> None:
    expired = subprocess.TimeoutExpired(["docker"], 5, output=b"pulling image\n", stderr=None)
    adapter = ComposeAdapter(RecordingRunner([expired]), locate=lambda _: "docker")

    outcome = adapter.start(
        project_name="devdock-s-0123456789ab",
        project_root=tmp_path,
        compose_file=tmp_path / "compose.yaml",
        timeout=5,
    )

    assert outcome.status == "timed_out"
    assert outcome.raw_stdout == "pulling image\n"
    assert outcome.raw_stderr == ""


def test_start_without_docker_is_unavailable_and_runs_nothing(tmp_path: Path) -> None:
    runner = RecordingRunner([])
    adapter = ComposeAdapter(runner, locate=lambda _: None)

    outcome = adapter.start(
        project_name="devdock-s-0123456789ab",
        project_root=tmp_path,
        compose_file=tmp_path / "compose.yaml",
        timeout=5,
    )

    assert outcome.status == "unavailable" and runner.calls == []


def test_start_rejects_an_unsafe_project_name_before_running_anything(tmp_path: Path) -> None:
    runner = RecordingRunner([])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    with pytest.raises(ValueError):
        adapter.start(
            project_name="evil name; rm -rf /",
            project_root=tmp_path,
            compose_file=tmp_path / "compose.yaml",
            timeout=5,
        )
    assert runner.calls == []


# --- declared services and observations ------------------------------------


def test_declared_services_are_read_from_compose_config_services(tmp_path: Path) -> None:
    runner = RecordingRunner([CommandResult(0, "web\ndb\n\n", "")])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    declared = adapter.list_declared_services(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )

    assert declared.names == ("web", "db") and declared.error is None
    args = runner.calls[0][0]
    assert args[-2:] == ["config", "--services"]
    assert "--quiet" not in args and "--project-name" not in args


def test_declared_services_failure_is_reported_without_output(tmp_path: Path) -> None:
    runner = RecordingRunner([CommandResult(1, "", "SECRET=leak invalid")])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    declared = adapter.list_declared_services(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )

    assert declared.names == () and declared.error is not None
    assert "exit code 1" in declared.error and "SECRET" not in repr(declared)


def observing(payload: CommandResult | BaseException) -> ComposeAdapter:
    return ComposeAdapter(RecordingRunner([payload]), locate=lambda _: "docker")


def observe(adapter: ComposeAdapter, tmp_path: Path):  # type: ignore[no-untyped-def]
    return adapter.observe_services(
        project_name="devdock-s-0123456789ab",
        project_root=tmp_path,
        compose_file=tmp_path / "compose.yaml",
    )


def test_observe_services_uses_project_scoped_compose_ps_json(tmp_path: Path) -> None:
    runner = RecordingRunner([CommandResult(0, "[]", "")])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    result = observe(adapter, tmp_path)

    assert result.items == () and result.error is None
    args = runner.calls[0][0]
    assert args[:2] == ["docker", "compose"]
    assert args[args.index("--project-name") + 1] == "devdock-s-0123456789ab"
    assert args[-5:] == ["ps", "--all", "--no-trunc", "--format", "json"]


ENTRY = (
    '{"ID":"abc","Name":"p-web-1","Service":"web","State":"running","Health":"healthy",'
    '"ExitCode":0,"Image":"private/registry:tag","Command":"serve --token=SECRET123",'
    '"Labels":"com.example.secret=LEAKED,other=x",'
    '"Publishers":[{"URL":"0.0.0.0","TargetPort":80,"PublishedPort":8080,"Protocol":"tcp"},'
    '{"URL":"","TargetPort":9000,"PublishedPort":0,"Protocol":"tcp"}]}'
)


@pytest.mark.parametrize(
    "payload",
    [
        f"[{ENTRY}]",
        f"{ENTRY}\n",
        f"{ENTRY}\n{ENTRY.replace('abc', 'def').replace('web-1', 'web-2')}\n",
    ],
    ids=["json-array", "ndjson-single", "ndjson-multiple"],
)
def test_observe_services_parses_both_compose_output_shapes(tmp_path: Path, payload: str) -> None:
    adapter = observing(CommandResult(0, payload, ""))

    result = observe(adapter, tmp_path)

    first = result.items[0]
    assert first.id == "abc" and first.service == "web"
    assert first.state == "running" and first.health == "healthy" and first.exit_code == 0
    assert first.ports == (PublishedPort("0.0.0.0", 8080, 80, "tcp"),)


def test_observe_services_discards_image_command_and_labels(tmp_path: Path) -> None:
    adapter = observing(CommandResult(0, ENTRY, ""))

    result = observe(adapter, tmp_path)

    rendered = repr(result)
    assert "SECRET123" not in rendered and "LEAKED" not in rendered
    assert "private/registry" not in rendered
    assert set(ServiceObservation.__dataclass_fields__) == {
        "id",
        "name",
        "service",
        "state",
        "health",
        "exit_code",
        "ports",
    }


def test_observe_services_skips_malformed_entries_and_counts_them(tmp_path: Path) -> None:
    payload = f'{ENTRY}\n{{"Name":"no-id"}}\n[1,2]\n'
    adapter = observing(CommandResult(0, payload, ""))

    result = observe(adapter, tmp_path)

    assert len(result.items) == 1 and result.skipped == 2 and result.error is None


def test_observe_services_reports_invalid_json_and_failures(tmp_path: Path) -> None:
    invalid = observing(CommandResult(0, "{not json", ""))
    failed = observing(CommandResult(1, "", "boom"))
    timed_out = observing(subprocess.TimeoutExpired(["docker"], 10))

    assert observe(invalid, tmp_path).error == "Service status output was not valid JSON."
    assert "exit code 1" in (observe(failed, tmp_path).error or "")
    assert "did not complete" in (observe(timed_out, tmp_path).error or "")


def test_observe_services_tolerates_missing_health_exit_code_and_publishers(
    tmp_path: Path,
) -> None:
    payload = '{"ID":"x","Name":"p-db-1","Service":"db","State":"Exited","ExitCode":true}'
    adapter = observing(CommandResult(0, payload, ""))

    item = observe(adapter, tmp_path).items[0]

    assert item.state == "exited" and item.health == "" and item.exit_code is None
    assert item.ports == ()


# --- stop -------------------------------------------------------------------


def test_stop_runs_plain_compose_down_scoped_to_the_project(tmp_path: Path) -> None:
    runner = RecordingRunner([OK])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")
    compose_file = tmp_path / "compose.yaml"

    outcome = adapter.stop(
        project_name="devdock-s-0123456789ab",
        project_root=tmp_path,
        compose_file=compose_file,
        timeout=60,
    )

    assert outcome.succeeded
    args, cwd, timeout = runner.calls[0]
    assert args == [
        "docker",
        "compose",
        "--project-directory",
        str(tmp_path),
        "--file",
        str(compose_file),
        "--project-name",
        "devdock-s-0123456789ab",
        "down",
    ]
    assert cwd == tmp_path and timeout == 60


def test_stop_never_removes_volumes_images_or_orphans(tmp_path: Path) -> None:
    runner = RecordingRunner([OK])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    adapter.stop(
        project_name="devdock-s-0123456789ab",
        project_root=tmp_path,
        compose_file=tmp_path / "compose.yaml",
        timeout=60,
    )

    args = runner.calls[0][0]
    for forbidden in ("--volumes", "-v", "--rmi", "--remove-orphans"):
        assert forbidden not in args


def test_stop_reports_failure_timeout_and_unavailability(tmp_path: Path) -> None:
    def stop_with(payload: CommandResult | BaseException, locate: str | None = "docker"):  # type: ignore[no-untyped-def]
        return ComposeAdapter(RecordingRunner([payload]), locate=lambda _: locate).stop(
            project_name="devdock-s-0123456789ab",
            project_root=tmp_path,
            compose_file=tmp_path / "compose.yaml",
            timeout=5,
        )

    failed = stop_with(CommandResult(1, "", "network in use"))
    timed_out = stop_with(subprocess.TimeoutExpired(["docker"], 5, output=b"stopping"))
    unavailable = stop_with(OK, locate=None)

    assert failed.status == "failed" and failed.raw_stderr == "network in use"
    assert timed_out.status == "timed_out" and timed_out.raw_stdout == "stopping"
    assert unavailable.status == "unavailable"


def test_stop_rejects_an_unsafe_project_name(tmp_path: Path) -> None:
    runner = RecordingRunner([])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    with pytest.raises(ValueError):
        adapter.stop(
            project_name="Bad Name",
            project_root=tmp_path,
            compose_file=tmp_path / "compose.yaml",
            timeout=5,
        )
    assert runner.calls == []
