"""Read small, non-secret project metadata files without executing project code."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

MAX_METADATA_BYTES = 1024 * 1024


@dataclass(frozen=True)
class MetadataValue:
    """A project metadata value and the filename it came from."""

    source: str
    value: str


@dataclass(frozen=True)
class PythonMetadata:
    """Python version declarations read from recognized metadata files."""

    declarations: tuple[MetadataValue, ...]
    errors: tuple[str, ...] = ()


def _read_text(root: Path, filename: str) -> str | None:
    path = root / filename
    if not path.is_file():
        return None
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root.resolve()):
            return None
        if resolved.stat().st_size > MAX_METADATA_BYTES:
            raise ValueError("file exceeds the 1 MiB metadata limit")
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{filename}: {exc}") from exc


def read_python_metadata(root: Path) -> PythonMetadata:
    """Read Python version declarations, never project code or env-file values."""

    declarations: list[MetadataValue] = []
    errors: list[str] = []
    try:
        pyproject = _read_text(root, "pyproject.toml")
    except ValueError as exc:
        pyproject = None
        errors.append(str(exc))
    if pyproject is not None:
        try:
            document = tomllib.loads(pyproject)
            requires_python = document.get("project", {}).get("requires-python")
            if isinstance(requires_python, str) and requires_python.strip():
                declarations.append(
                    MetadataValue("pyproject.toml: project.requires-python", requires_python)
                )
        except (tomllib.TOMLDecodeError, AttributeError) as exc:
            errors.append(f"pyproject.toml: {exc}")

    for filename in (".python-version", ".tool-versions"):
        try:
            contents = _read_text(root, filename)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if contents is None:
            continue
        if filename == ".python-version":
            version = contents.strip()
            if version:
                declarations.append(MetadataValue(filename, version))
        else:
            for line in contents.splitlines():
                fields = line.partition("#")[0].split()
                if len(fields) >= 2 and fields[0] == "python":
                    declarations.append(MetadataValue(filename, fields[1]))
                    break
    return PythonMetadata(tuple(declarations), tuple(errors))
