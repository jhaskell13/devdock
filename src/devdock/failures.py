"""Classification of runtime failures into stable, actionable codes.

A failed ``docker compose`` run reports its reason in prose that DevDock
treats as untrusted: it can contain interpolated configuration, registry
hostnames, and values Compose read from the project's ``.env``. This module
reads that text to recognize *which kind* of failure occurred, then throws it
away. Everything it returns is written here in this file -- a code, a summary,
and a remedy -- so classification can never become a disclosure channel.

Matching uses lowercase literal substrings rather than regular expressions:
the markers come from runtime output an attacker can influence, and literal
containment has no backtracking behavior to exploit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Operation = Literal["UP", "DOWN"]
FailureKind = Literal[
    "permission_denied",
    "daemon_unavailable",
    "config_invalid",
    "registry_auth",
    "image_not_found",
    "pull_failed",
    "port_conflict",
    "network_conflict",
    "resource_exhausted",
    "resource_in_use",
    "service_failed",
    "unknown",
]

PERMISSION_DENIED_REMEDY = (
    "Your user cannot open the Docker socket. On Linux or WSL, add yourself to the 'docker' "
    "group and then start a new login session (on WSL: run 'wsl --shutdown' from Windows and "
    "reopen the terminal); on Docker Desktop, confirm WSL integration is enabled for this "
    "distribution. Then retry."
)

# Checked in order; the first kind with a matching marker wins. More specific
# kinds come before the general ones they would otherwise be mistaken for.
_MARKERS: tuple[tuple[FailureKind, tuple[str, ...]], ...] = (
    (
        "permission_denied",
        (
            "permission denied while trying to connect",
            "dial unix /var/run/docker.sock: connect: permission denied",
            "got permission denied",
            "access is denied",
        ),
    ),
    (
        "daemon_unavailable",
        (
            "cannot connect to the docker daemon",
            "is the docker daemon running",
            "error during connect",
            "the system cannot find the file specified",
            "docker daemon is not running",
        ),
    ),
    (
        "config_invalid",
        (
            "yaml: ",
            "services must be a mapping",
            "additional property",
            "is not allowed",
            "validating ",
            "invalid compose project",
            "unsupported config option",
            "non-string key",
            "undefined volume",
            "undefined network",
            "service refers to undefined",
            "depends on undefined service",
        ),
    ),
    (
        "registry_auth",
        (
            "pull access denied",
            "authentication required",
            "unauthorized: incorrect username or password",
            "requested access to the resource is denied",
            "no basic auth credentials",
            "login attempt",
        ),
    ),
    (
        "image_not_found",
        (
            "manifest unknown",
            "manifest for ",
            "repository does not exist",
            "not found: manifest unknown",
            "no such image",
            "image not found",
            "failed to resolve reference",
        ),
    ),
    (
        "pull_failed",
        (
            "failed to pull",
            "error pulling image",
            "tls handshake timeout",
            "i/o timeout",
            "net/http: request canceled",
            "temporary failure in name resolution",
            "no route to host",
            "proxyconnect tcp",
            "toomanyrequests",
            "download failed",
        ),
    ),
    (
        "port_conflict",
        (
            "port is already allocated",
            "address already in use",
            "bind: address already in use",
            "ports are not available",
            "failed to bind host port",
            "only one usage of each socket address",
        ),
    ),
    (
        "network_conflict",
        (
            "pool overlaps with other one",
            "network with name",
            "all predefined address pools have been fully subnetted",
            "failed to create network",
        ),
    ),
    (
        "resource_exhausted",
        (
            "no space left on device",
            "cannot allocate memory",
            "disk quota exceeded",
            "too many open files",
        ),
    ),
    (
        "resource_in_use",
        (
            "has active endpoints",
            "volume is in use",
            "is in use by container",
            "resource is still in use",
            "removal of container",
        ),
    ),
    (
        "service_failed",
        (
            "dependency failed to start",
            "exited with code",
            "container is unhealthy",
            "is unhealthy",
            "error while creating mount source path",
            "no such file or directory",
            "exec: ",
            "oci runtime create failed",
        ),
    ),
)

_SUFFIX: dict[FailureKind, str] = {
    "permission_denied": "PERMISSION_DENIED",
    "daemon_unavailable": "DAEMON_UNAVAILABLE",
    "config_invalid": "CONFIG_INVALID",
    "registry_auth": "REGISTRY_AUTH_REQUIRED",
    "image_not_found": "IMAGE_NOT_FOUND",
    "pull_failed": "PULL_FAILED",
    "port_conflict": "PORT_CONFLICT",
    "network_conflict": "NETWORK_CONFLICT",
    "resource_exhausted": "RESOURCE_EXHAUSTED",
    "resource_in_use": "RESOURCE_IN_USE",
    "service_failed": "SERVICE_FAILED",
    "unknown": "FAILED",
}

_SUMMARY: dict[FailureKind, str] = {
    "permission_denied": "Docker refused the connection for this user.",
    "daemon_unavailable": "The Docker daemon stopped responding during the operation.",
    "config_invalid": "The Compose configuration was rejected as invalid.",
    "registry_auth": "The image registry refused the request without valid credentials.",
    "image_not_found": "A referenced image or tag does not exist in the registry.",
    "pull_failed": "An image could not be downloaded.",
    "port_conflict": "A published port is already in use on this machine.",
    "network_conflict": "A Docker network could not be created or already exists.",
    "resource_exhausted": "The host ran out of a resource such as disk space or memory.",
    "resource_in_use": "A resource is still in use and could not be removed.",
    "service_failed": "A service failed to start or became unhealthy.",
    "unknown": "The runtime reported an error DevDock does not recognize.",
}

_REMEDY: dict[FailureKind, str] = {
    "permission_denied": PERMISSION_DENIED_REMEDY,
    "daemon_unavailable": "Start Docker Desktop or the Docker Engine service, confirm with "
    "'devdock check', then retry.",
    "config_invalid": "Run 'docker compose config' in the project to see the validation error, "
    "correct the file, then retry.",
    "registry_auth": "Run 'docker login' for the registry that holds the image, then retry. "
    "DevDock does not store credentials.",
    "image_not_found": "Correct the image name or tag in the Compose file, or push the image, "
    "then retry.",
    "pull_failed": "Check network and proxy access to the registry, then retry. Offline work "
    "needs the images present locally.",
    "port_conflict": "Stop whatever holds the port or publish a different one. List declared "
    "ports in devdock.yaml and run 'devdock check' to probe them.",
    "network_conflict": "Remove the conflicting network or adjust the Compose network "
    "configuration, then retry.",
    "resource_exhausted": "Free disk space or memory, then retry. 'docker system df' shows what "
    "Docker is using.",
    "resource_in_use": "Stop whatever is still using the resource, confirm with "
    "'devdock status', then retry.",
    "service_failed": "Inspect the service's own logs with 'docker compose logs', correct the "
    "service, then retry.",
    "unknown": "Rerun with --show-output to see the runtime's own message, then correct the "
    "reported problem.",
}

# Classification reads at most this much output. Beyond it, a failure is simply
# unrecognized; no marker scan is worth an unbounded scan of hostile text.
MAX_CLASSIFIED_CHARACTERS = 256_000


@dataclass(frozen=True)
class FailureExplanation:
    """A recognized failure, described only in DevDock's own words."""

    kind: FailureKind
    code: str
    summary: str
    remedy: str

    @property
    def recognized(self) -> bool:
        return self.kind != "unknown"


def classify_failure(operation: Operation, stdout: str, stderr: str) -> FailureExplanation:
    """Identify the kind of failure in captured output without quoting any of it."""

    text = f"{stderr}\n{stdout}"[:MAX_CLASSIFIED_CHARACTERS].lower()
    kind: FailureKind = "unknown"
    for candidate, markers in _MARKERS:
        if any(marker in text for marker in markers):
            kind = candidate
            break
    return FailureExplanation(
        kind=kind,
        code=f"{operation}_{_SUFFIX[kind]}",
        summary=_SUMMARY[kind],
        remedy=_REMEDY[kind],
    )
