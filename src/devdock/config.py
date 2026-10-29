"""Loading and validation for a project's DevDock profile."""

from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


def _has_control_characters(text: str) -> bool:
    """Control characters in a path would corrupt labels and line-oriented tool output."""

    return any(ord(character) < 32 or ord(character) == 127 for character in text)


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
