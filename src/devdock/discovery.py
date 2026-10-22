"""Project root and common environment-file discovery."""

from pathlib import Path

CONFIG_NAME = "devdock.yaml"
COMPOSE_NAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)


def discover_project(start: Path) -> Path:
    """Find the nearest parent containing a DevDock profile or Compose file."""

    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_NAME).is_file() or any(
            (candidate / name).is_file() for name in COMPOSE_NAMES
        ):
            return candidate
    return current

