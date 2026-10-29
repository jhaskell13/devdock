# Changelog

Notable user-facing changes are recorded here. The project is pre-release; entries are grouped under `Unreleased` until a version is published.

## Unreleased

### Added

- A gated Docker integration suite (`pytest -m docker`) that exercises the real runtime with disposable resources and verifies the Compose labels, `compose ps` JSON fields, and failure-taxonomy markers that unit tests can only fake. Deselected by default; skips when no daemon is reachable.
- `examples/demo`, a disposable sample project, and `scripts/demo.py`, a scripted walkthrough of the full lifecycle including the ownership refusal and volume preservation.
- `python -m devdock` as an alternative to the installed script.

- Failure taxonomy for `up` and `down`: permission, daemon, configuration, registry auth, image, pull, port, network, host-resource, in-use, and service failures each get a stable code and a specific suggested action. Recognized failures withhold the runtime's output, since the code already explains the problem.
- Shape-based redaction for credentials DevDock has never seen, including private keys, JWTs, cloud and provider tokens, `Bearer` headers, and secret-named `key=value` pairs.
- `CODE_OF_CONDUCT.md`.

### Fixed

- `devdock init` templated the project name into YAML unquoted, so a directory named `true`, `1.0`, or `a: b` produced a profile that failed validation. The profile is now serialized.
- `down` reported `DOWN_PARTIAL` after correctly leaving an undeclared running container in place, because that container kept the project network alive. The plan now says the network will stay, and the verification expects it.
- `UP_TIMED_OUT` referred to a `docker ps` command that was not always printed. It is now included in the evidence.
- The `DOCKER_DAEMON_AVAILABLE` evidence still mentioned `docker info` after the probe switched to `docker version`.
- `up` and `down` printed the runtime's raw progress output on success, contradicting the documented contract and showing duplicated Compose lines. Output on success now waits behind `--show-output`, as documented.
- A port probe that failed during `up` preflight printed as `[FAIL]` inside an `[OK]` result. It now prints as `[ADVISORY]` with an explanation that it did not block the start.
- The failure taxonomy did not recognize `context deadline exceeded`, Go's standard timeout wording and what Docker reports for a registry it cannot reach, so an unreachable registry fell through to `UP_FAILED`. Registry connection markers were added and verified against a real daemon.
- DevDock reported `DOCKER_DAEMON_AVAILABLE` when the daemon socket could not be opened. The probe relied on the exit status of `docker info`, which is zero even when the server is unreachable, so `check` passed, `up` proceeded to refuse with an ownership conflict, and the Docker test suite ran instead of skipping. Reachability now requires a server version from `docker version`, and a socket the user may not open is reported as `DOCKER_DAEMON_PERMISSION_DENIED` with the group and re-login remedy. The suite and the demo use the same detection.
- `devdock up` refused to run a second time when the profile declared a port its own services publish. Port probes are now advisory during the start preflight; Compose's own bind remains authoritative and reports `UP_PORT_CONFLICT`.
- Redaction could stall on long runtime output. The URL-credential pattern had an unbounded scheme run and backtracked quadratically; 256 KB of plain text took over a minute. All patterns are now bounded and a regression test asserts linear behavior.
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

