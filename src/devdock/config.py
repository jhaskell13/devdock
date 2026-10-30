"""Loading and validation for a project's DevDock profile."""

import ipaddress
import re
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Compose's own service-name shape, restated here rather than inherited from the
# Compose file. These names become argv elements when a start is scoped to
# individual services, so a value beginning with "-" would be read as an option.
# The length cap keeps a name usable as a DNS label, which is what Compose makes
# of it on the project network.
SERVICE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")

PROBE_TIMEOUT_SECONDS = 2.0
MAX_PROBE_TIMEOUT_SECONDS = 60.0
MAX_PATH_LENGTH = 2000

DependencyCondition = Literal[
    "service_started",
    "service_healthy",
    "service_completed_successfully",
    "service_ready",
]


def _has_control_characters(text: str) -> bool:
    """Control characters in a path would corrupt labels and line-oriented tool output."""

    return any(ord(character) < 32 or ord(character) == 127 for character in text)


def _is_loopback(host: str) -> bool:
    """Whether ``host`` can only address this machine.

    Readiness probes are the first DevDock feature that opens a connection to an
    address the profile chose, so a profile that arrives with a cloned
    repository must not be able to direct that connection anywhere else.
    Anything not provably loopback is rejected when the profile loads, and the
    probe re-checks before connecting. ``localhost`` is accepted and resolved by
    the host; DevDock does not override name resolution.
    """

    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ReadinessProbeBase(BaseModel):
    """Fields every readiness probe shares: where to connect and for how long."""

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: Annotated[int, Field(strict=True, ge=1, le=65535)]
    timeout: Annotated[float, Field(gt=0, le=MAX_PROBE_TIMEOUT_SECONDS)] = PROBE_TIMEOUT_SECONDS

    @field_validator("host")
    @classmethod
    def host_must_be_loopback(cls, value: str) -> str:
        if not _is_loopback(value):
            raise ValueError(
                "readiness probes may only target loopback: 127.0.0.1, ::1, or localhost"
            )
        return value


class TcpReadiness(ReadinessProbeBase):
    """Ready when a TCP connection to a published host port is accepted.

    ``port`` is the port as published on the host, not the port inside the
    container; DevDock probes from the host and cannot see container ports.
    """

    type: Literal["tcp"]


class HttpReadiness(ReadinessProbeBase):
    """Ready when one plain-HTTP request to a published host port answers acceptably.

    Only ``http://`` is supported. A TLS endpoint should use a ``tcp`` probe
    until DevDock defines certificate-verification behavior.
    """

    type: Literal["http"]
    path: str = "/"
    method: Literal["GET", "HEAD"] = "GET"
    expect_status: list[Annotated[int, Field(strict=True, ge=100, le=599)]] | None = None

    @field_validator("path")
    @classmethod
    def path_must_be_an_absolute_request_target(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("path must start with '/'")
        if len(value) > MAX_PATH_LENGTH:
            raise ValueError(f"path must be at most {MAX_PATH_LENGTH} characters")
        # Whitespace and control characters would split the request line, which
        # would let a profile append headers of its own choosing.
        if _has_control_characters(value) or any(character.isspace() for character in value):
            raise ValueError("path must not contain whitespace or control characters")
        return value

    @field_validator("expect_status")
    @classmethod
    def expected_statuses_must_not_be_empty(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and not value:
            raise ValueError(
                "expect_status must list at least one status code; omit it to accept 200-399"
            )
        return value

    def accepts(self, status: int) -> bool:
        """Whether ``status`` satisfies this probe. The default range is 200-399."""

        if self.expect_status is None:
            return 200 <= status < 400
        return status in self.expect_status


ReadinessProbe = Annotated[TcpReadiness | HttpReadiness, Field(discriminator="type")]


class ServiceProfile(BaseModel):
    """DevDock's additions to one Compose service: when it may start, when it is ready.

    Both fields are additive. ``depends_on`` is merged with the Compose file's
    own dependencies rather than replacing them, and ``readiness`` is checked
    alongside the container's Compose health check when it has one.
    """

    model_config = ConfigDict(extra="forbid")

    depends_on: dict[str, DependencyCondition] = Field(default_factory=dict)
    readiness: list[ReadinessProbe] = Field(default_factory=list)

    @field_validator("depends_on", mode="before")
    @classmethod
    def accept_the_compose_short_form(cls, value: object) -> object:
        """A bare list of names is Compose's short form and means ``service_started``."""

        if not isinstance(value, list):
            return value
        if not all(isinstance(item, str) for item in value):
            return value  # let the declared type report the error
        if len(set(value)) != len(value):
            raise ValueError("depends_on must not name a service more than once")
        return dict.fromkeys(value, "service_started")

    @field_validator("depends_on")
    @classmethod
    def dependency_names_must_be_service_names(
        cls, value: dict[str, DependencyCondition]
    ) -> dict[str, DependencyCondition]:
        for name in value:
            if not SERVICE_NAME.fullmatch(name):
                raise ValueError(f"{name!r} is not a valid Compose service name")
        return value


class ProjectProfile(BaseModel):
    """The version 1 project configuration format."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    project: str = Field(min_length=1)
    runtimes: dict[str, str] = Field(default_factory=dict)
    compose_file: Path | None = None
    required_env: list[str] = Field(default_factory=list)
    required_files: list[Path] = Field(default_factory=list)
    required_dirs: list[Path] = Field(default_factory=list)
    ports: list[Annotated[int, Field(strict=True, ge=1, le=65535)]] = Field(default_factory=list)
    services: dict[str, ServiceProfile] = Field(default_factory=dict)

    @field_validator("project")
    @classmethod
    def project_name_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("project must contain a non-whitespace character")
        return value

    @field_validator("runtimes")
    @classmethod
    def runtime_names_must_be_normalized(cls, value: dict[str, str]) -> dict[str, str]:
        for name, requirement in value.items():
            if name != name.lower() or not name.strip():
                raise ValueError("runtime names must be non-empty lowercase identifiers")
            if not requirement.strip():
                raise ValueError(f"runtime requirement for {name!r} must not be empty")
        return value

    @field_validator("compose_file")
    @classmethod
    def compose_file_must_stay_inside_project(cls, value: Path | None) -> Path | None:
        if value is None:
            return value
        raw_path = str(value)
        windows_path = PureWindowsPath(raw_path)
        if _has_control_characters(raw_path):
            raise ValueError("compose_file must not contain control characters")
        if value.is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise ValueError("compose_file must be a relative path inside the project root")
        if ".." in value.parts or ".." in windows_path.parts:
            raise ValueError("compose_file must not escape the project root")
        if "\\" in raw_path:
            raise ValueError("compose_file must use forward slashes for cross-platform paths")
        return value

    @field_validator("required_env")
    @classmethod
    def environment_names_must_be_valid(cls, value: list[str]) -> list[str]:
        for name in value:
            if not name.isidentifier():
                raise ValueError(f"{name!r} is not a valid environment variable name")
        return value

    @field_validator("required_files", "required_dirs")
    @classmethod
    def required_paths_must_stay_inside_project(cls, value: list[Path]) -> list[Path]:
        for path in value:
            raw_path = str(path)
            windows_path = PureWindowsPath(raw_path)
            if (
                _has_control_characters(raw_path)
                or path.is_absolute()
                or windows_path.is_absolute()
                or windows_path.drive
                or ".." in path.parts
                or ".." in windows_path.parts
                or "\\" in raw_path
            ):
                raise ValueError(
                    "required paths must be relative, project-contained forward-slash paths"
                )
        return value

    @field_validator("ports")
    @classmethod
    def ports_must_not_repeat(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("ports must not contain duplicates")
        return value

    @field_validator("services")
    @classmethod
    def service_keys_must_be_service_names(
        cls, value: dict[str, ServiceProfile]
    ) -> dict[str, ServiceProfile]:
        for name in value:
            if not SERVICE_NAME.fullmatch(name):
                raise ValueError(f"{name!r} is not a valid Compose service name")
        return value

    @field_validator("services")
    @classmethod
    def a_service_must_not_depend_on_itself(
        cls, value: dict[str, ServiceProfile]
    ) -> dict[str, ServiceProfile]:
        """Reject the one cycle that needs no graph to see.

        Longer cycles span the Compose file's dependencies too, so they are
        reported once the full graph is built rather than here.
        """

        for name, service in value.items():
            if name in service.depends_on:
                raise ValueError(f"service {name!r} must not depend on itself")
        return value


class ProfileError(Exception):
    """A readable configuration loading or validation error."""


def load_profile(path: Path) -> ProjectProfile:
    """Load and validate YAML profile data from ``path``."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ProfileError(str(exc)) from exc

    try:
        return ProjectProfile.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'/'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ProfileError(details) from exc
