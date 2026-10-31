# Contributing

Thanks for helping improve DevDock. By participating you agree to the [code of conduct](CODE_OF_CONDUCT.md). The project is early and its command and profile formats may still change, so open an issue or discussion before making large or compatibility-sensitive changes.

## Development setup

Use Python 3.12 or newer. On Windows, the setup script selects the newest installed Python 3 version through the `py` launcher, creates `.venv`, and installs the development dependencies:

```powershell
.\scripts\dev-setup.ps1
```

If the existing environment needs to be rebuilt, run `.scripts\dev-setup.ps1 -Recreate`. To use a particular installed version, specify both `-Recreate` and its launcher selector, for example `.scripts\dev-setup.ps1 -Recreate -PythonSelector 3.15`.

On Linux or macOS, use `python3 -m venv .venv` and `source .venv/bin/activate`, then install with `python -m pip install -e '.[dev]'`.

## Before submitting a change

Run the checks from the repository root. On Windows, use the virtual environment's interpreter directly so commands cannot accidentally use a different Python:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m ruff format --check src tests
.\.venv\Scripts\python.exe -m mypy src/devdock
.\.venv\Scripts\python.exe -m build
```

On Linux or macOS, use the equivalent `python -m ...` commands after activating `.venv`. Code is formatted with `ruff format`; run it before committing, or install the optional pre-commit hooks from `.pre-commit-config.yaml`.

### Test suites

The default run never needs Docker:

```
python -m pytest
```

The Docker suite is marked `docker` and deselected by default. With a daemon running:

```
python -m pytest -m docker
```

It spends almost all of its time waiting on containers, so it parallelises
well. Each test derives its own Compose project name from its own temporary
directory, and each worker takes its own slice of the host port range:

```sh
python -m pytest -m docker -n 4
```

The default suite runs in about twelve seconds, where `-n` would cost more in
start-up than it saves, so nothing is parallelised unless you ask for it.

It creates only disposable containers, networks, and volumes in a temporary directory, and removes them in fixture teardown, including the volumes DevDock itself preserves. It skips rather than fails when no daemon is reachable. `python scripts/demo.py` runs the same ground as a readable walkthrough.

Add or update tests when behavior changes. Prefer synthetic temporary project directories and fake adapters. Unit tests must not require a running Docker daemon, real credentials, personal data, or access to production systems.

Keep changes focused and make commits that describe one meaningful step. Use the `type(scope): summary` pattern when it fits; common types are `feat`, `fix`, `docs`, `test`, `ci`, and `build`. Update `CHANGELOG.md` under `Unreleased` for user-visible changes, and update the README, CLI contract, architecture notes, or roadmap when behavior or planned scope changes. Do not commit virtual environments, tool caches, build outputs, `.env` files, keys, or generated state.

## Pull requests

Explain the user problem, the behavior changed, how it was verified, and any limitations. Include command output or screenshots only when they clarify a user-visible change; redact secrets and private paths.

