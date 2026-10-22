# DevDock

DevDock is a local development environment manager. Its goal is to make software projects easier to inspect, configure, start, troubleshoot, and stop on a developer's own machine.

The repository foundation milestone is complete. The current CLI can create a profile, discover project files, validate that profile, and check a small set of declared prerequisites. It does not start or stop services yet.

## What DevDock does today

| Command | What it does | Changes the project or host? |
| --- | --- | --- |
| `devdock init [DIRECTORY]` | Creates a starter `devdock.yaml`. The directory defaults to the current directory. | Creates one profile file. |
| `devdock inspect [DIRECTORY]` | Finds the nearest project profile or common Compose file and reports what it found. | No. |
| `devdock check [DIRECTORY]` | Validates the profile and compares the running Python interpreter with its declared minimum; checks Docker Compose CLI, Compose file, and required environment variable names. | No. |
| `devdock --help` | Shows available commands and options. | No. |

See [`docs/cli.md`](docs/cli.md) for exact discovery, output, and exit-code behavior.

`check` verifies that the Docker CLI and Compose plugin respond to `docker compose version`; it does not check whether the Docker daemon is running or start containers. Required environment variables must be set in the process environment. DevDock does not load `.env` files or print environment variable values.

## Requirements

- Python 3.12 or newer.
- Docker Desktop or Docker Engine with the Compose plugin only for projects that declare `docker: compose`.

## Install for development

Clone the repository, then create and activate a virtual environment.

**Windows PowerShell**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

**Linux or macOS**

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Confirm the installation:

```text
devdock --help
```

If `py -3.12` or `python3.12` is unavailable, install Python 3.12 or newer and make sure its launcher is on your `PATH`.

## Use DevDock in a project

From the project directory, create and inspect a starter profile:

```text
devdock init
devdock inspect
devdock check
```

Or pass a directory explicitly:

```text
devdock init path/to/project
devdock inspect path/to/project
devdock check path/to/project
```

`inspect` and `check` search upward from the supplied directory for the nearest `devdock.yaml` or common Compose file. `init` creates `devdock.yaml` in the directory you pass; it refuses to overwrite an existing profile.

### Configure `devdock.yaml`

The starter profile is deliberately permissive. Add only the requirements that apply to the project. For example:

```yaml
schema_version: 1
project: sample-web-app
runtimes:
  python: ">=3.12"
  docker: "compose"
compose_file: compose.yaml
required_env:
  - APP_MODE
```

The complete starter example is in [`devdock.example.yaml`](devdock.example.yaml); field behavior and design limits are described in [`docs/architecture.md`](docs/architecture.md).

| Field | Required | Meaning in the current CLI |
| --- | --- | --- |
| `schema_version` | Yes | Must be the integer `1`. |
| `project` | Yes | A non-empty display name. |
| `runtimes` | No | A mapping of supported runtime identifiers to requirements. `python` accepts a minimum such as `>=3.12`; `docker` accepts `compose`. Unknown runtime identifiers are reported as unsupported. |
| `compose_file` | No | A forward-slash path relative to the project root; absolute paths and lexical `..` traversal are rejected. `check` verifies that the file exists. |
| `required_env` | No | Names that must be set in the environment inherited by DevDock. Values are never displayed. |

Unknown profile fields are rejected so spelling mistakes do not silently pass validation. The current schema does not interpolate variables, load `.env`, or execute profile-provided commands.

To set an environment variable for a check:

```powershell
$env:APP_MODE = "development"
devdock check
```

```sh
export APP_MODE=development
devdock check
```

Do not commit real credentials, `.env` files, customer data, or other sensitive information. Keep local secrets out of the repository and its Git history.

## Results and exit codes

- Exit code `0`: the profile is valid and all checks it declares passed.
- Exit code `1`: the profile is missing or invalid, or at least one check failed. Findings include a suggested action when DevDock can provide one.
- CLI usage errors use Typer's argument parsing exit behavior.

The checks are local diagnostics, not a guarantee that an application will start. Docker daemon health, service readiness, networking, volumes, and teardown are future roadmap work.

## Troubleshooting

**`devdock` is not recognized**

Activate the virtual environment where you installed DevDock. If it is not installed yet, run `python -m pip install -e ".[dev]"` from the repository root.

**`devdock check` cannot find a profile**

Run `devdock init` in the intended project directory, or pass the directory containing the profile to `devdock check`.

**A runtime is reported as unsupported**

The first implementation checks Python minimum versions and Docker Compose availability. Other runtime adapters are planned; see [`ROADMAP.md`](ROADMAP.md).

**A required variable is reported as missing**

Set it in the shell before launching DevDock. The current CLI checks the inherited process environment and intentionally does not parse `.env` files.

## Contributing and development checks

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the local workflow. From an activated development environment, run:

```text
python -m pytest
python -m ruff check src tests
python -m mypy src/devdock
```

DevDock is intended to use synthetic disposable projects for automated checks. Tests must not require production systems, customer records, real credentials, or access to a running Docker daemon unless a future integration suite explicitly documents an isolated disposable setup.

## Project plans

[`ROADMAP.md`](ROADMAP.md) lays out the engineering and acceptance criteria needed to reach a carefully validated release.
User-visible changes are tracked in [`CHANGELOG.md`](CHANGELOG.md).

## License

DevDock is distributed under the MIT License. See [`LICENSE`](LICENSE).

