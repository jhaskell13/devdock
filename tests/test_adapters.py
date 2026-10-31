import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
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
        self.environments: list[Mapping[str, str] | None] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float,
        env: Mapping[str, str] | None = None,
        cancel: object = None,
    ) -> CommandResult:
        self.calls.append((list(args), cwd, timeout))
        self.environments.append(env)
        result = next(self._results)
        if isinstance(result, BaseException):
            raise result
        return result


OK = CommandResult(0, "", "")
# A daemon probe that actually answered. The probe requires a server version on
# stdout, so an empty success is deliberately *not* treated as reachable.
DAEMON_UP = CommandResult(0, "27.3.1\n", "")


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
    runner = RecordingRunner([OK, DAEMON_UP])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    capabilities = adapter.detect_capabilities()

    assert capabilities.compose_available and capabilities.daemon_available
    assert [call[0] for call in runner.calls] == [
        ["docker", "compose", "version"],
        ["docker", "version", "--format", "{{.Server.Version}}"],
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
    runner = RecordingRunner([OK, DAEMON_UP, OK])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    findings = check_profile(profile, tmp_path, adapter=adapter)
    codes = {item.code for item in findings}

    assert {"COMPOSE_CLI_AVAILABLE", "DOCKER_DAEMON_AVAILABLE", "COMPOSE_CONFIG_VALID"} <= codes
    assert len(runner.calls) == 3


def test_default_runner_never_uses_a_shell() -> None:
    """Proved by behavior rather than by inspecting the call.

    Shell metacharacters in an argument are only inert if no shell ever sees
    them, so the test passes some to a real child and checks that it received
    them unchanged. That holds the same claim on Windows, where a shell would
    be cmd.exe and the quoting rules are different.
    """

    hostile = "a; rm -rf / && echo $HOME `id` | cat > /tmp/x"

    result = run_command(
        (sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", hostile),
        timeout=30,
    )

    assert result.returncode == 0
    assert result.stdout == hostile


# --- resource inventory ---------------------------------------------------


def container_line(*fields: str) -> str:
    return "\t".join(json.dumps(field) for field in fields)


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
        assert "{{json .}}" not in fmt and "inspect" not in args  # never whole objects
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
            CommandResult(
                0, container_line("n1", "devdock-s-0123456789ab_default") + "\nmalformed\n", ""
            ),
            CommandResult(0, json.dumps("devdock-s-0123456789ab_data") + "\n", ""),
        ]
    )
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    inventory = adapter.list_project_resources("devdock-s-0123456789ab")

    first, second = inventory.containers
    assert first.id == "id1" and first.service == "web" and first.parse_error is None
    assert first.config_files == "/proj/compose.yaml"
    assert second.parse_error is not None and second.name == ""
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
        "--remove-orphans=false",
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
        "--remove-orphans=false",
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
    assert "--remove-orphans=false" in args


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


# --- hardening ----------------------------------------------------------------


def test_every_runtime_call_pins_compose_settings_the_project_env_could_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMPOSE_REMOVE_ORPHANS", "true")  # a hostile shell/.env value
    runner = RecordingRunner([OK] * 8)
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    adapter.detect_capabilities()
    adapter.list_project_resources("devdock-s-0123456789ab")
    adapter.start(
        project_name="devdock-s-0123456789ab",
        project_root=tmp_path,
        compose_file=tmp_path / "compose.yaml",
        timeout=1,
    )
    adapter.stop(
        project_name="devdock-s-0123456789ab",
        project_root=tmp_path,
        compose_file=tmp_path / "compose.yaml",
        timeout=1,
    )

    assert runner.environments and all(env is not None for env in runner.environments)
    for env in runner.environments:
        assert env is not None
        assert env["COMPOSE_REMOVE_ORPHANS"] == "false"
        assert env["COMPOSE_ANSI"] == "never"
    assert "PATH" in (runner.environments[0] or {})  # inherits the rest of the environment


def test_tabs_and_newlines_inside_labels_cannot_forge_extra_records() -> None:
    hostile_service = 'web\tFalse\n"forged"\t"x"\t"running"\t"p"\t"/p"\t"/p/c.yaml"\t"db"\t"False"'
    line = container_line(
        "id1", "p-web-1", "running", "p", "/p", "/p/compose.yaml", hostile_service, "False"
    )
    runner = RecordingRunner([CommandResult(0, line + "\n", ""), OK, OK, OK])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    inventory = adapter.list_project_resources("devdock-s-0123456789ab")

    assert len(inventory.containers) == 1
    assert inventory.containers[0].service == hostile_service
    assert inventory.containers[0].parse_error is None


def test_non_string_or_invalid_json_fields_become_parse_errors() -> None:
    bad_type = "\t".join(["1", *[json.dumps("x")] * 7])
    bad_json = "\t".join(["{oops", *[json.dumps("x")] * 7])
    runner = RecordingRunner([CommandResult(0, f"{bad_type}\n{bad_json}\n", ""), OK, OK, OK])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    inventory = adapter.list_project_resources("devdock-s-0123456789ab")

    assert all(item.parse_error for item in inventory.containers)


def test_container_template_json_encodes_every_field() -> None:
    from devdock.adapters.compose import _CONTAINER_FORMAT, _NETWORK_FORMAT, _VOLUME_FORMAT

    for template in (_CONTAINER_FORMAT, _NETWORK_FORMAT, _VOLUME_FORMAT):
        fields = template.split("\t")
        assert all(field.startswith("{{json (") and field.endswith(")}}") for field in fields)


# --- daemon probe ------------------------------------------------------------


def test_daemon_probe_does_not_trust_a_zero_exit_with_no_server_version() -> None:
    """`docker info` exits 0 with an unreachable socket; the probe must not be fooled.

    This is the false positive that made DevDock report DOCKER_DAEMON_AVAILABLE
    on a machine whose user could not open the socket at all.
    """

    runner = RecordingRunner([OK, CommandResult(0, "", "")])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    capabilities = adapter.detect_capabilities()

    assert capabilities.compose_available
    assert capabilities.daemon == "unavailable"
    assert not capabilities.daemon_available


@pytest.mark.parametrize(
    "stderr",
    [
        "permission denied while trying to connect to the Docker daemon socket at "
        "unix:///var/run/docker.sock: dial unix /var/run/docker.sock: connect: permission denied",
        "error during connect: open //./pipe/docker_engine: Access is denied.",
    ],
)
def test_daemon_probe_distinguishes_permission_problems(stderr: str) -> None:
    runner = RecordingRunner([OK, CommandResult(1, "", stderr)])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    capabilities = adapter.detect_capabilities()

    assert capabilities.daemon == "permission_denied"
    assert not capabilities.daemon_available
    assert "docker.sock" not in repr(capabilities)  # classified, never quoted


def test_daemon_probe_reports_a_stopped_daemon_as_unavailable() -> None:
    stopped = CommandResult(
        1,
        "",
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
        "Is the docker daemon running?",
    )
    adapter = ComposeAdapter(RecordingRunner([OK, stopped]), locate=lambda _: "docker")

    assert adapter.detect_capabilities().daemon == "unavailable"


# --- dependency listing -------------------------------------------------

# A rendered configuration shaped like the one a real Compose emits, including
# the fields DevDock must discard. The secret-looking values are here so the
# tests can assert that none of them survives parsing.
CONFIG_JSON = json.dumps(
    {
        "name": "sample",
        "networks": {"default": {"name": "sample_default"}},
        "services": {
            "web": {
                "image": "nginx:alpine",
                "environment": {
                    "SECRET_TOKEN": "${MY_SECRET}",
                    "RESOLVED": "super-secret-from-env-file",
                },
                "env_file": [{"path": "/tmp/extra.env", "required": True}],
                "volumes": [{"type": "bind", "source": "/home/someone/code", "target": "/app"}],
                "depends_on": {
                    "db": {"condition": "service_healthy", "required": True},
                    "migrate": {
                        "condition": "service_completed_successfully",
                        "required": True,
                    },
                    "cache": {"condition": "service_started", "required": False},
                },
            },
            "db": {"image": "postgres:16", "healthcheck": {"test": ["CMD", "true"]}},
            "migrate": {"image": "busybox"},
            "cache": {"image": "redis", "healthcheck": {"disable": True}},
        },
    }
)


def _dependency_adapter(result: CommandResult) -> tuple[ComposeAdapter, RecordingRunner]:
    runner = RecordingRunner([result])
    return ComposeAdapter(runner, locate=lambda _: "docker"), runner


def test_dependency_listing_asks_compose_not_to_interpolate(tmp_path: Path) -> None:
    """Without this flag Compose renders env_file contents and ${VAR} into the output."""

    adapter, runner = _dependency_adapter(CommandResult(0, CONFIG_JSON, ""))

    adapter.list_dependencies(project_root=tmp_path, compose_file=tmp_path / "compose.yaml")

    args, cwd, timeout = runner.calls[0]
    assert args[:2] == ["docker", "compose"]
    assert args[-4:] == ["config", "--format", "json", "--no-interpolate"]
    assert (cwd, timeout) == (tmp_path, 10.0)


def test_dependency_listing_keeps_only_the_ordering_facts(tmp_path: Path) -> None:
    adapter, _ = _dependency_adapter(CommandResult(0, CONFIG_JSON, ""))

    declared = adapter.list_dependencies(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )

    assert declared.usable
    assert declared.services == ("cache", "db", "migrate", "web")
    assert [
        (item.service, item.depends_on, item.condition, item.required)
        for item in declared.dependencies
    ] == [
        ("web", "cache", "service_started", False),
        ("web", "db", "service_healthy", True),
        ("web", "migrate", "service_completed_successfully", True),
    ]
    assert declared.healthcheck_declared == frozenset({"db"})
    assert declared.healthcheck_disabled == frozenset({"cache"})


def test_nothing_from_the_configuration_survives_except_those_facts(tmp_path: Path) -> None:
    """The document describes images, mounts, and environment. None may be retained."""

    adapter, _ = _dependency_adapter(CommandResult(0, CONFIG_JSON, ""))

    declared = adapter.list_dependencies(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )

    rendered = repr(declared)
    for leaked in (
        "MY_SECRET",
        "super-secret-from-env-file",
        "/tmp/extra.env",
        "/home/someone/code",
        "nginx:alpine",
        "postgres:16",
        "SECRET_TOKEN",
    ):
        assert leaked not in rendered, f"{leaked} survived parsing"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (CommandResult(1, "", "services.web: secret value /home/me/.env failed"), "exit code 1"),
        (CommandResult(0, "not json at all", ""), "not valid JSON"),
        (CommandResult(0, '"a string"', ""), "not an object"),
        (CommandResult(0, '{"services": []}', ""), "services as an object"),
        (CommandResult(0, '{"services": {"web": "nope"}}', ""), "a service in a form"),
    ],
)
def test_unreadable_configurations_report_the_failure_not_the_content(
    tmp_path: Path, result: CommandResult, expected: str
) -> None:
    adapter, _ = _dependency_adapter(result)

    declared = adapter.list_dependencies(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )

    assert declared.error is not None
    assert expected in declared.error
    assert not declared.usable


def test_a_failed_listing_never_quotes_the_runtime_message(tmp_path: Path) -> None:
    hostile = "error: DATABASE_URL=postgres://user:hunter2@localhost/db is invalid"
    adapter, _ = _dependency_adapter(CommandResult(1, hostile, hostile))

    declared = adapter.list_dependencies(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )

    assert declared.error == "Compose dependency listing failed with exit code 1."
    assert "hunter2" not in repr(declared)


def test_an_oversized_configuration_is_refused_rather_than_parsed(tmp_path: Path) -> None:
    adapter, _ = _dependency_adapter(CommandResult(0, "{" + " " * 4_000_001, ""))

    declared = adapter.list_dependencies(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )

    assert declared.error is not None and "too large" in declared.error


def test_the_short_list_form_is_still_understood(tmp_path: Path) -> None:
    """Current Compose normalizes it away; an older one may not."""

    document = json.dumps(
        {"services": {"web": {"depends_on": ["db", "cache"]}, "db": {}, "cache": {}}}
    )
    adapter, _ = _dependency_adapter(CommandResult(0, document, ""))

    declared = adapter.list_dependencies(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )

    assert [(item.depends_on, item.condition) for item in declared.dependencies] == [
        ("cache", "service_started"),
        ("db", "service_started"),
    ]


def test_a_condition_this_version_cannot_order_against_is_reported(tmp_path: Path) -> None:
    """A newer Compose must not be silently downgraded to 'started'."""

    document = json.dumps(
        {"services": {"web": {"depends_on": {"db": {"condition": "service_warm"}}}, "db": {}}}
    )
    adapter, _ = _dependency_adapter(CommandResult(0, document, ""))

    declared = adapter.list_dependencies(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )

    assert not declared.usable
    assert declared.error is None
    assert declared.unsupported == (
        "Compose reported the dependency condition service_warm, which this version of "
        "DevDock cannot order against.",
    )


def test_an_unprintable_condition_is_described_rather_than_echoed(tmp_path: Path) -> None:
    document = json.dumps(
        {"services": {"web": {"depends_on": {"db": {"condition": "\x1b[2Jwiped"}}}, "db": {}}}
    )
    adapter, _ = _dependency_adapter(CommandResult(0, document, ""))

    declared = adapter.list_dependencies(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )

    assert declared.unsupported == (
        "Compose reported the dependency condition an unprintable value, which this version "
        "of DevDock cannot order against.",
    )
    # The unrecognized value itself is the one field here that came from
    # outside a fixed vocabulary, so it must not be retained on the edge.
    assert "wiped" not in repr(declared)
    assert declared.dependencies == ()


@pytest.mark.parametrize("name", ["bad name", "web\nservice", "sql\x1b[2J", "", "üñî"])
def test_service_names_compose_could_not_have_produced_are_refused(
    tmp_path: Path, name: str
) -> None:
    document = json.dumps({"services": {name: {}}})
    adapter, _ = _dependency_adapter(CommandResult(0, document, ""))

    declared = adapter.list_dependencies(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )

    assert declared.error is not None and "DevDock does not recognize" in declared.error
    assert name == "" or name not in declared.error


def test_a_dependency_naming_an_unrecognizable_service_is_refused(tmp_path: Path) -> None:
    document = json.dumps({"services": {"web": {"depends_on": {"bad name": {}}}}})
    adapter, _ = _dependency_adapter(CommandResult(0, document, ""))

    declared = adapter.list_dependencies(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )

    assert declared.error is not None and "dependency of service web" in declared.error
    assert "bad name" not in declared.error


def test_a_project_with_no_services_key_is_empty_not_an_error(tmp_path: Path) -> None:
    adapter, _ = _dependency_adapter(CommandResult(0, '{"name": "sample"}', ""))

    declared = adapter.list_dependencies(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )

    assert declared.usable and declared.services == ()


def test_a_missing_docker_cli_reports_it_without_running_anything(tmp_path: Path) -> None:
    runner = RecordingRunner([])
    adapter = ComposeAdapter(runner, locate=lambda _: None)

    declared = adapter.list_dependencies(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )

    assert declared.error == "Docker CLI is not available."
    assert runner.calls == []


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (OSError("boom"), "did not complete"),
        (subprocess.TimeoutExpired(["docker"], 10.0), "did not complete"),
    ],
)
def test_a_listing_that_cannot_run_reports_it_without_the_exception_text(
    tmp_path: Path, failure: BaseException, expected: str
) -> None:
    runner = RecordingRunner([failure])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")

    declared = adapter.list_dependencies(
        project_root=tmp_path, compose_file=tmp_path / "compose.yaml"
    )

    assert declared.error is not None and expected in declared.error
    assert "boom" not in declared.error


@pytest.mark.parametrize(
    ("reader", "expected"),
    [
        ("list_dependencies", "Compose configuration output was nested too deeply to parse."),
        ("observe_services", "Service status output was nested too deeply to parse."),
    ],
)
def test_json_nested_too_deeply_is_refused_rather_than_raised(
    tmp_path: Path, reader: str, expected: str
) -> None:
    """A RecursionError is not a JSONDecodeError and would escape as a traceback.

    Compose echoes `x-` extension fields verbatim and they accept any
    structure, so the nesting can come from a project file.
    """

    deep = '{"services": {"web": ' + "[" * 20000 + "]" * 20000 + "}}"
    runner = RecordingRunner([CommandResult(0, deep, "")])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")
    arguments: dict[str, object] = {
        "project_root": tmp_path,
        "compose_file": tmp_path / "compose.yaml",
    }
    if reader == "observe_services":
        arguments["project_name"] = "devdock-sample-0123456789ab"

    result = getattr(adapter, reader)(**arguments)

    assert result.error == expected


# --- scoped starts --------------------------------------------------------


def _start(**kwargs: object) -> list[str]:
    runner = RecordingRunner([CommandResult(0, "", "")])
    adapter = ComposeAdapter(runner, locate=lambda _: "docker")
    adapter.start(
        project_name="devdock-sample-0123456789ab",
        project_root=Path("/p"),
        compose_file=Path("/p/compose.yaml"),
        **kwargs,  # type: ignore[arg-type]
    )
    return runner.calls[0][0]


def test_an_unscoped_start_names_no_services() -> None:
    args = _start()

    assert args[-3:] == ["up", "--detach", "--remove-orphans=false"]
    assert "--" not in args and "--no-deps" not in args


def test_a_scoped_start_puts_service_names_after_a_double_dash() -> None:
    """Without it, Compose reads a service named "-weird" as flags.

    Verified against a real Compose: it answers "unknown shorthand flag: 'e'"
    without the separator and "no such service" with it.
    """

    args = _start(services=("db", "web"))

    assert args[-3:] == ["--", "db", "web"]
    assert args.index("--") > args.index("up")


def test_a_scoped_start_can_be_told_not_to_pull_in_dependencies() -> None:
    assert "--no-deps" in _start(services=("web",), no_deps=True)
    assert "--no-deps" not in _start(services=("web",), no_deps=False)


def test_the_parallel_limit_precedes_the_subcommand() -> None:
    """It is one of Compose's global options, so it is invalid after `up`."""

    args = _start(max_parallel=3)

    assert args[:4] == ["docker", "compose", "--parallel", "3"]
    assert args.index("--parallel") < args.index("up")


def test_no_parallel_limit_means_no_flag() -> None:
    assert "--parallel" not in _start()


@pytest.mark.parametrize("name", ["bad name", "web;rm -rf /", "web\nservice", ""])
def test_a_service_name_that_is_not_argv_safe_is_refused(name: str) -> None:
    """The separator makes a leading dash inert; this refuses the rest."""

    with pytest.raises(ValueError, match="not safe"):
        _start(services=(name,))


def test_a_name_longer_than_63_characters_is_accepted() -> None:
    """Compose has no length limit, and an invented one broke real projects."""

    long_name = "a" * 80

    assert _start(services=(long_name,))[-1] == long_name


def test_a_leading_dash_is_allowed_because_the_separator_makes_it_inert() -> None:
    """Compose accepts such a name, so DevDock must not reject the project."""

    args = _start(services=("-leading",))

    assert args[-2:] == ["--", "-leading"]
