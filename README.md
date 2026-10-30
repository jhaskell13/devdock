# DevDock

DevDock is a local development environment manager. Its goal is to make software projects easier to inspect, configure, start, troubleshoot, and stop on a developer's own machine.

Milestones 0 through 2 are complete. The CLI can create a profile, discover project files, validate the profile, check declared prerequisites, and start, report on, and stop a project's Docker Compose services while proving which resources it owns. It does not yet wait for services to become ready or recover interrupted operations; see [`ROADMAP.md`](ROADMAP.md).

## What DevDock does today

| Command | What it does | Changes the project or host? |
| --- | --- | --- |
| `devdock init [DIRECTORY]` | Creates a starter `devdock.yaml`. The directory defaults to the current directory. | Creates one profile file. |
| `devdock inspect [DIRECTORY]` | Finds the nearest project profile or common Compose file, reports the marker used, nested roots, and known project files. | No. |
| `devdock check [DIRECTORY]` | Validates the profile and checks declared Python, Docker/Compose, paths, ports, and environment requirements. Reports warnings and prerequisite-dependent checks it could not run. | No. |
| `devdock up [DIRECTORY]` | Runs the checks, proves which Compose resources DevDock owns, then starts the project's services detached. Refuses before acting if any gate fails. | Starts containers and networks for the derived Compose project. |
| `devdock status [DIRECTORY]` | Reports each service's container, state, health, readiness class, and published ports. Keeps "running" distinct from "ready". | No. |
| `devdock down [DIRECTORY]` | Shows a plan, asks for confirmation (or `--yes`), then stops and removes the project's DevDock-managed containers and networks. `--dry-run` previews only. | Removes containers and networks it created. Never volumes. |
| `devdock --help` | Shows available commands and options. | No. |

See [`docs/cli.md`](docs/cli.md) for exact discovery, output, and exit-code behavior.

`check` verifies that the Docker CLI and Compose plugin respond, that the daemon answers `docker version`, and that the Compose file parses; it does not start containers. Required environment variables must be set in the process environment. DevDock does not load `.env` files or print environment variable values.

## Requirements

- Python 3.12 or newer.
- Docker Desktop or Docker Engine with the Compose plugin only for projects that declare `docker: compose`.

## Install for development

Clone the repository, then run the setup script from the repository root. It uses the newest Python 3 installation available through the Windows `py` launcher (or `python` as a fallback), verifies that it is Python 3.12 or newer, creates `.venv` if needed, and installs DevDock with its development tools.

**Windows PowerShell**

```powershell
.\scripts\dev-setup.ps1
```

Run the full test suite separately:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

If the existing `.venv` is broken or uses an older Python, recreate it explicitly with `.scripts\dev-setup.ps1 -Recreate`. This removes only the repository's `.venv` directory before rebuilding it. To select a Python version, use both flags, for example `.scripts\dev-setup.ps1 -Recreate -PythonSelector 3.15`.

On Linux or macOS, create and install into a virtual environment with:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

Then run tests with `python -m pytest` while that environment is active. Confirm the installation:

```text
devdock --help
```

DevDock requires Python 3.12 or newer. The setup script prints the available Python launcher options if it cannot find a suitable interpreter.

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

`inspect` and `check` search upward from the supplied directory for the nearest `devdock.yaml`, common Compose file, or supported project manifest. `inspect` reports the evidence and inventories known project files. `init` creates `devdock.yaml` in the directory you pass; it refuses to overwrite an existing profile.

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
required_files:
  - pyproject.toml
required_dirs:
  - src
ports:
  - 8000
```

The complete starter example is in [`devdock.example.yaml`](devdock.example.yaml); field behavior and design limits are described in [`docs/architecture.md`](docs/architecture.md).

| Field | Required | Meaning in the current CLI |
| --- | --- | --- |
| `schema_version` | Yes | Must be the integer `1`. |
| `project` | Yes | A non-empty display name. |
| `runtimes` | No | A mapping of supported runtime identifiers to requirements. `python` accepts a minimum such as `>=3.12`; `docker` accepts `compose`. Python checks report DevDock's executable and version. Unknown runtime identifiers are reported as unsupported. |
| `compose_file` | No | A forward-slash path relative to the project root; absolute paths and lexical `..` traversal are rejected. `check` verifies that the file exists and resolves inside the project. |
| `required_env` | No | Names that must be set to a non-empty value in the environment inherited by DevDock. Unset and empty values are distinguished; values are never displayed. |
| `required_files` | No | Project-relative files that must exist. Absolute paths, backslashes, and lexical traversal are rejected. |
| `required_dirs` | No | Project-relative directories that must exist. Symlinks resolving outside the project fail the check. |
| `ports` | No | TCP ports from 1 through 65535 to probe on `127.0.0.1`. Availability is a point-in-time observation and may change immediately. |
| `services` | No | Per-service `depends_on` conditions and `readiness` probes. Validated when the profile loads; **no command acts on them yet**. They will drive `devdock up --wait`; see [`ROADMAP.md`](ROADMAP.md). |

Unknown profile fields are rejected so spelling mistakes do not silently pass validation. The current schema does not interpolate variables, load `.env`, or execute profile-provided commands. Readiness probes are limited to TCP and HTTP against loopback; there is no probe type that runs a command.

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

- Exit code `0`: the profile is valid and no check failed. Warnings or skipped dependent checks may be reported when DevDock cannot verify a target interpreter or prerequisite.
- Exit code `1`: the profile is missing or invalid, or at least one check failed. Findings include a suggested action when DevDock can provide one.
- CLI usage errors use Typer's argument parsing exit behavior.

`devdock up` starts the project's Compose services detached once the diagnostics pass and DevDock can prove every resource under its derived project name is its own. It does not wait for services to become ready; `devdock status` shows per-service state, health, and a readiness class that keeps running distinct from ready. `devdock down` previews and confirms before stopping containers and networks, and never removes volumes. The checks are local diagnostics, not a guarantee that an application will start. Compose checks distinguish CLI/plugin response, daemon reachability, and Compose parsing. Output from Docker commands is hidden on success and for recognized failures; unrecognized failures show a redacted tail, and `--show-output` shows everything, still redacted. Readiness waiting, dependency ordering, recovery of interrupted operations, and any volume removal remain future roadmap work. Finding codes and evidence are documented in [`docs/cli.md`](docs/cli.md#diagnostic-codes).

## Troubleshooting

**`devdock` is not recognized**

Activate the virtual environment where you installed DevDock. If it is not installed yet, run `python -m pip install -e ".[dev]"` from the repository root.

**`DOCKER_DAEMON_PERMISSION_DENIED`, or every Docker test skips with a permission message**

The Docker CLI works but your user may not open the daemon socket. On Linux or WSL, add yourself to the `docker` group, then start a new login session; group changes do not apply to shells that are already open. On WSL, run `wsl --shutdown` from Windows and reopen the terminal. If Docker Desktop is in use, confirm WSL integration is enabled for this distribution. `id` should then list `docker` among your groups.

**`devdock check` cannot find a profile**

Run `devdock init` in the intended project directory, or pass the directory containing the profile to `devdock check`.

**A runtime is reported as unsupported**

Docker Compose is the only runtime adapter. Python checks compare the interpreter running DevDock against the declared minimum. Other runtime adapters are planned; see [`ROADMAP.md`](ROADMAP.md).

**A required variable is reported as missing**

Set it in the shell before launching DevDock. The current CLI checks the inherited process environment and intentionally does not parse `.env` files.

## Try it

With Docker running, from the repository root:

```bash
python scripts/demo.py
```

This copies `examples/demo` to a temporary directory and walks through the whole lifecycle: discovery, checks, status before and after starting, an idempotent repeat of `up` and `down`, a dry-run preview, a refusal to touch a container DevDock did not create, and the volume that survives teardown. It removes everything it created. See [`examples/demo/README.md`](examples/demo/README.md) to run the steps by hand.

## Code of conduct

Participation is covered by the [code of conduct](CODE_OF_CONDUCT.md).

## Contributing and development checks

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the local workflow. From the repository root on Windows, run:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src/devdock
```

DevDock is intended to use synthetic disposable projects for automated checks. Tests must not require production systems, customer records, or real credentials. The default suite never needs a Docker daemon; the separately selected `docker` suite creates only disposable local resources and removes them afterwards, as documented in [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Project plans

[`ROADMAP.md`](ROADMAP.md) lays out the engineering and acceptance criteria needed to reach a carefully validated release.
User-visible changes are tracked in [`CHANGELOG.md`](CHANGELOG.md).

## License

DevDock is distributed under the MIT License. See [`LICENSE`](LICENSE).

