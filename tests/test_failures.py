import pytest

from devdock.failures import (
    MAX_CLASSIFIED_CHARACTERS,
    FailureKind,
    classify_failure,
)

# One realistic message per kind, as Docker or Compose actually phrases them.
SAMPLES: list[tuple[FailureKind, str]] = [
    (
        "permission_denied",
        "Got permission denied while trying to connect to the Docker daemon socket at "
        "unix:///var/run/docker.sock",
    ),
    (
        "daemon_unavailable",
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
        "Is the docker daemon running?",
    ),
    (
        "config_invalid",
        "validating /srv/app/compose.yaml: services.web Additional property buidl is not allowed",
    ),
    (
        "registry_auth",
        "Error response from daemon: pull access denied for internal/api, repository does not "
        "exist or may require 'docker login'",
    ),
    (
        "image_not_found",
        "Error response from daemon: manifest for registry.example.com/app:v9 not found: "
        "manifest unknown",
    ),
    (
        "pull_failed",
        "failed to pull image nginx:latest: net/http: TLS handshake timeout",
    ),
    (
        "port_conflict",
        "Error starting userland proxy: listen tcp4 0.0.0.0:8080: bind: address already in use",
    ),
    (
        "network_conflict",
        "failed to create network app_default: Pool overlaps with other one on this address space",
    ),
    ("resource_exhausted", "write /var/lib/docker/tmp/x: no space left on device"),
    (
        "resource_in_use",
        "error while removing network: network app_default has active endpoints",
    ),
    ("service_failed", "dependency failed to start: container app-db-1 exited with code 1"),
]


@pytest.mark.parametrize(("expected", "message"), SAMPLES, ids=[kind for kind, _ in SAMPLES])
def test_each_failure_kind_is_recognized(expected: FailureKind, message: str) -> None:
    failure = classify_failure("UP", "", message)

    assert failure.kind == expected
    assert failure.recognized
    assert failure.code == f"UP_{failure.code.split('_', 1)[1]}"
    assert failure.summary and failure.remedy


@pytest.mark.parametrize(("expected", "message"), SAMPLES, ids=[kind for kind, _ in SAMPLES])
def test_classification_never_quotes_the_runtime_output(
    expected: FailureKind, message: str
) -> None:
    secret = "hunter2-very-secret"
    failure = classify_failure("UP", f"{message} {secret}", f"token={secret}")

    assert secret not in repr(failure)
    for word in ("compose.yaml", "registry.example.com", "/var/lib/docker", "app-db-1"):
        assert word not in repr(failure)


def test_unrecognized_output_falls_back_without_claiming_knowledge() -> None:
    failure = classify_failure("UP", "", "something entirely novel went wrong")

    assert failure.kind == "unknown"
    assert not failure.recognized
    assert failure.code == "UP_FAILED"
    assert "does not recognize" in failure.summary
    assert "--show-output" in failure.remedy


def test_operation_selects_the_code_prefix() -> None:
    message = "network app_default has active endpoints"

    assert classify_failure("DOWN", "", message).code == "DOWN_RESOURCE_IN_USE"
    assert classify_failure("UP", "", message).code == "UP_RESOURCE_IN_USE"


def test_more_specific_kinds_win_over_general_ones() -> None:
    # Mentions both an auth failure and a missing repository; auth is the actionable one.
    both = "pull access denied for app, repository does not exist or may require 'docker login'"

    assert classify_failure("UP", "", both).kind == "registry_auth"


def test_stderr_is_preferred_but_stdout_is_still_scanned() -> None:
    assert classify_failure("UP", "no space left on device", "").kind == "resource_exhausted"


def test_classification_is_case_insensitive() -> None:
    assert classify_failure("UP", "", "PORT IS ALREADY ALLOCATED").kind == "port_conflict"


def test_classification_is_bounded_for_hostile_output() -> None:
    padding = "x" * (MAX_CLASSIFIED_CHARACTERS + 5000)

    assert classify_failure("UP", "", padding).kind == "unknown"
    assert classify_failure("UP", "", "port is already allocated" + padding).kind == "port_conflict"


def test_every_kind_has_a_distinct_code_summary_and_remedy() -> None:
    kinds = [kind for kind, _ in SAMPLES] + ["unknown"]
    failures = [classify_failure("UP", "", "") for _ in kinds]
    explained = [classify_failure("UP", "", message) for _, message in SAMPLES]

    codes = {item.code for item in explained}
    assert len(codes) == len(SAMPLES)
    assert all(failure.code == "UP_FAILED" for failure in failures)
