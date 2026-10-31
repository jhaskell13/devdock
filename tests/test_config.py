from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from devdock.config import (
    HttpReadiness,
    ProfileError,
    ProjectProfile,
    TcpReadiness,
    load_profile,
)


def _with_services(services: object) -> ProjectProfile:
    return ProjectProfile.model_validate(
        {"schema_version": 1, "project": "sample", "services": services}
    )


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


def test_services_default_to_empty_so_existing_profiles_keep_working() -> None:
    profile = ProjectProfile.model_validate({"schema_version": 1, "project": "sample"})

    assert profile.services == {}


def test_reads_dependencies_and_readiness_probes(tmp_path: Path) -> None:
    profile_path = tmp_path / "devdock.yaml"
    profile_path.write_text(
        "schema_version: 1\n"
        "project: sample\n"
        "services:\n"
        "  web:\n"
        "    depends_on:\n"
        "      db: service_healthy\n"
        "      migrate: service_completed_successfully\n"
        "    readiness:\n"
        "      - type: http\n"
        "        port: 8080\n"
        "        path: /healthz\n"
        "  db:\n"
        "    readiness:\n"
        "      - type: tcp\n"
        "        port: 5432\n"
        "        timeout: 1.5\n",
        encoding="utf-8",
    )

    profile = load_profile(profile_path)

    web = profile.services["web"]
    assert web.depends_on == {
        "db": "service_healthy",
        "migrate": "service_completed_successfully",
    }
    probe = web.readiness[0]
    assert isinstance(probe, HttpReadiness)
    assert (probe.host, probe.port, probe.path) == ("127.0.0.1", 8080, "/healthz")
    assert probe.method == "GET"
    database = profile.services["db"].readiness[0]
    assert isinstance(database, TcpReadiness)
    assert (database.port, database.timeout) == (5432, 1.5)


def test_compose_short_form_dependencies_mean_service_started() -> None:
    profile = _with_services({"web": {"depends_on": ["db", "cache"]}})

    assert profile.services["web"].depends_on == {
        "db": "service_started",
        "cache": "service_started",
    }


def test_rejects_a_service_named_twice_in_the_short_form() -> None:
    with pytest.raises(ValidationError, match="more than once"):
        _with_services({"web": {"depends_on": ["db", "db"]}})


def test_rejects_an_unknown_dependency_condition() -> None:
    with pytest.raises(ValidationError, match="depends_on"):
        _with_services({"web": {"depends_on": {"db": "service_warm"}}})


def test_rejects_a_service_that_depends_on_itself() -> None:
    with pytest.raises(ValidationError, match="depend on itself"):
        _with_services({"web": {"depends_on": {"web": "service_started"}}})


@pytest.mark.parametrize("name", ["", "web service", "web\nservice", "wéb", "a/b"])
def test_rejects_unsafe_service_names(name: str) -> None:
    with pytest.raises(ValidationError, match="valid Compose service name"):
        _with_services({name: {}})


@pytest.mark.parametrize("name", ["db cache", "db\tcache", "db\x1b[2J", "db/cache"])
def test_rejects_unsafe_dependency_names(name: str) -> None:
    with pytest.raises(ValidationError, match="valid Compose service name"):
        _with_services({"web": {"depends_on": {name: "service_started"}}})


def test_rejects_unknown_fields_inside_a_service() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _with_services({"web": {"wait_for": ["db"]}})


def test_rejects_unknown_fields_inside_a_probe() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _with_services({"web": {"readiness": [{"type": "tcp", "port": 80, "retries": 3}]}})


@pytest.mark.parametrize(
    "host",
    ["example.com", "10.0.0.1", "0.0.0.0", "169.254.169.254", "127.0.0.1.example.com", ""],
)
def test_readiness_probes_may_only_target_loopback(host: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        _with_services({"web": {"readiness": [{"type": "tcp", "port": 80, "host": host}]}})


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost", "LOCALHOST"])
def test_loopback_hosts_are_accepted(host: str) -> None:
    profile = _with_services({"web": {"readiness": [{"type": "tcp", "port": 80, "host": host}]}})

    assert profile.services["web"].readiness[0].host == host


@pytest.mark.parametrize("probe_type", ["tcp", "http"])
@pytest.mark.parametrize("port", [0, 65536, True, "8080"])
def test_rejects_invalid_probe_ports(probe_type: str, port: object) -> None:
    with pytest.raises(ValidationError):
        _with_services({"web": {"readiness": [{"type": probe_type, "port": port}]}})


@pytest.mark.parametrize("probe_type", [None, "", "exec", "grpc"])
def test_rejects_unsupported_probe_types(probe_type: object) -> None:
    with pytest.raises(ValidationError):
        _with_services({"web": {"readiness": [{"type": probe_type, "port": 80}]}})


@pytest.mark.parametrize("timeout", [0, -1, 61])
def test_probe_timeouts_must_be_positive_and_bounded(timeout: float) -> None:
    with pytest.raises(ValidationError, match="timeout"):
        _with_services({"web": {"readiness": [{"type": "tcp", "port": 80, "timeout": timeout}]}})


@pytest.mark.parametrize(
    "path", ["healthz", "/health z", "/health\r\nX-Injected: 1", "/health\x00", "/" + "a" * 2000]
)
def test_rejects_request_paths_that_could_split_the_request_line(path: str) -> None:
    with pytest.raises(ValidationError, match="path"):
        _with_services({"web": {"readiness": [{"type": "http", "port": 80, "path": path}]}})


def test_rejects_an_empty_expected_status_list() -> None:
    with pytest.raises(ValidationError, match="at least one status code"):
        _with_services({"web": {"readiness": [{"type": "http", "port": 80, "expect_status": []}]}})


def test_expected_statuses_default_to_the_success_and_redirect_range() -> None:
    profile = _with_services({"web": {"readiness": [{"type": "http", "port": 80}]}})
    probe = profile.services["web"].readiness[0]
    assert isinstance(probe, HttpReadiness)

    assert [probe.accepts(status) for status in (199, 200, 399, 400, 503)] == [
        False,
        True,
        True,
        False,
        False,
    ]


def test_declared_statuses_replace_the_default_range() -> None:
    profile = _with_services(
        {"web": {"readiness": [{"type": "http", "port": 80, "expect_status": [204, 418]}]}}
    )
    probe = profile.services["web"].readiness[0]
    assert isinstance(probe, HttpReadiness)

    assert [probe.accepts(status) for status in (204, 418, 200)] == [True, True, False]


@pytest.mark.parametrize("name", ["_internal", "-leading-dash", "web.1", "a", "a" * 64])
def test_accepts_every_service_name_compose_itself_accepts(name: str) -> None:
    """A narrower rule would reject projects that Compose runs.

    A leading dash is legal to Compose, so it is legal here; argv safety is
    enforced where a name becomes a command argument, not by refusing the
    profile. There is no length limit either: Compose imposes none, and a cap
    of 63 characters once made `up` fail on a project Compose starts happily.
    """

    assert name in _with_services({name: {}}).services


@pytest.mark.parametrize("path", ["/café", "/日本", "/naïve"])
def test_rejects_non_ascii_request_paths(path: str) -> None:
    """They reach the HTTP client and raise there, which is a crash, not a result."""

    with pytest.raises(ValidationError, match="ASCII"):
        _with_services({"web": {"readiness": [{"type": "http", "port": 80, "path": path}]}})


def test_rejects_a_fragment_in_a_request_path() -> None:
    """It is stripped before sending, so the reported target would not be the one probed."""

    with pytest.raises(ValidationError, match="fragment"):
        _with_services(
            {"web": {"readiness": [{"type": "http", "port": 80, "path": "/health#frag"}]}}
        )


def test_the_number_of_probes_per_service_is_capped() -> None:
    """Each is attempted serially on every status report and every poll of a wait."""

    probes = [{"type": "tcp", "port": 1000 + index} for index in range(9)]

    with pytest.raises(ValidationError, match="at most 8"):
        _with_services({"web": {"readiness": probes}})
