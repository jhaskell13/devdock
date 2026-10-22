# Contributing

Thanks for helping improve DevDock. The project is early and its command and profile formats may still change, so open an issue or discussion before making large or compatibility-sensitive changes.

## Development setup

Use Python 3.12 or newer:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

On Linux or macOS, use `python3.12 -m venv .venv` and `source .venv/bin/activate`.

## Before submitting a change

Run the checks from the repository root:

```text
python -m pytest
python -m ruff check src tests
python -m mypy src/devdock
python -m build
```

Add or update tests when behavior changes. Prefer synthetic temporary project directories and fake adapters. Unit tests must not require a running Docker daemon, real credentials, personal data, or access to production systems.

Keep changes focused and make commits that describe one meaningful step. Use the `type(scope): summary` pattern when it fits; common types are `feat`, `fix`, `docs`, `test`, `ci`, and `build`. Update `CHANGELOG.md` under `Unreleased` for user-visible changes, and update the README, CLI contract, architecture notes, or roadmap when behavior or planned scope changes. Do not commit virtual environments, tool caches, build outputs, `.env` files, keys, or generated state.

## Pull requests

Explain the user problem, the behavior changed, how it was verified, and any limitations. Include command output or screenshots only when they clarify a user-visible change; redact secrets and private paths.

