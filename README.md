# DevDock

DevDock is a local development environment manager. It aims to make projects predictable to inspect, check, start, troubleshoot, and stop, beginning with a small Python CLI and Docker Compose.

This repository is at its initial implementation milestone. The first increment establishes the CLI contract and a validated project profile; service orchestration and persistent environment history are later milestones.

## Development setup

Requires Python 3.12 or newer.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
devdock --help
```

On Linux or macOS, activate the environment with `source .venv/bin/activate`.

## First commands

Run commands from a project directory:

```text
devdock init
devdock inspect
devdock check
```

`devdock init` creates `devdock.yaml`. `inspect` reports the discovered project files. `check` validates the profile and checks declared local prerequisites. It does not start containers or modify the host environment.

## Project profile

See [`devdock.example.yaml`](devdock.example.yaml) for the initial profile format. The profile may declare Python and Docker Compose requirements, a Compose file, and required environment variable **names**. Secret values belong in a local `.env` file and must never be committed.

## Roadmap

- Configuration schema and project discovery
- Runtime and dependency diagnostics
- Docker Compose lifecycle (`up`, `status`, `down`)
- Readiness checks, dependency ordering, and timeouts
- Drift snapshots and safe recovery
- Additional runtime adapters, platform coverage, packaging, and documentation

