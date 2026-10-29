# Changelog

Notable user-facing changes are recorded here. The project is pre-release; entries are grouped under `Unreleased` until a version is published.

## Unreleased

### Added

- Hardening from the Milestone 2 review: Compose runs with `COMPOSE_REMOVE_ORPHANS` pinned off so a project `.env` cannot widen `down`; `down` re-verifies the plan immediately before acting and reports `DOWN_PARTIAL` when it cannot confirm the result; inventory fields are JSON-encoded so labels cannot forge records; profile paths reject control characters; redaction skips well-known non-secret variables and cannot mangle its own placeholders.
- `devdock --version`. Terminal output neutralizes control characters from container names and runtime data. The package ships a `py.typed` marker.
- `devdock down` previews, confirms, then stops and removes a project's DevDock-managed containers and networks, re-checks what remains, and never removes volumes. Supports `--dry-run` and `--yes`.
- `devdock status` reports each declared service's container, state, Compose health, readiness class, and published ports, lists resources DevDock does not own, and distinguishes running from ready.
- `devdock up` starts a project's Compose services detached after passing diagnostic, Compose-file, runtime, and ownership gates. Captured Docker output is hidden by default, redacted on every path, and shown in full only with `--show-output`.
- Ownership scope: a deterministic Compose project name per project root, printed by `devdock inspect`, and label-based rules that future lifecycle commands use to refuse resources DevDock did not create.
- Structured discovery evidence, nested-root reporting, and safe project manifest/Python metadata inspection.
- Coded diagnostics for Python interpreter evidence, Docker/Compose availability and parsing, required paths, environment state, and TCP ports.
- Explicit skipped-check findings when Docker prerequisites or safe Compose inputs are unavailable; Docker command output is not exposed.
- `devdock init`, `devdock inspect`, and `devdock check` for local project profiles.
- Version 1 YAML profile validation and read-only checks for declared Python, Docker Compose, Compose file, and environment-name requirements.
- CLI behavior, architecture, installation, contribution, and security documentation.
- Cross-platform quality workflow for Python 3.12–3.14 on Linux, macOS, and Windows.

