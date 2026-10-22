# DevDock roadmap

This roadmap describes the engineering scope to turn the first CLI prototype into a reliable, documented, cross-platform local development tool. It is deliberately broader than the shortest path to a demo: the difficult work is safe lifecycle management, recovery from partial failures, clear diagnostics, and verifying behavior across real operating systems and disposable projects.

The plan includes implementation, design, code review, tests, documentation, packaging, and platform validation. AI assistance can speed up individual tasks, but it does not remove the need to understand failures, inspect generated code, and verify behavior on the target systems.

## Product principles

- Operate on the user's local project and local runtimes; no cloud control plane.
- Read configuration as data. Never execute arbitrary commands just because they appear in a profile.
- Make checks explain what failed, why DevDock knows that, and what the user can do next.
- Scope lifecycle actions to resources DevDock can identify as managed.
- Preview destructive operations and require explicit authorization before removing persistent data.
- Use disposable synthetic fixtures. Do not require personal, customer, regulated, production, or confidential data.
- Prefer recoverable state transitions and idempotent commands over hidden background behavior.
- Keep the first user interface as a CLI; defer a web dashboard and product-embedded AI features.

## Milestones

| Milestone | Result |
| --- | --- |
| 0. Repository and CLI contract | Clear install/use docs, stable command behavior, versioned profile schema, baseline CI. |
| 1. Discovery and diagnostics (in progress) | Actionable, structured checks for files, runtimes, ports, configuration, and environment requirements. |
| 2. Compose adapter and lifecycle | Safe `up`, `status`, and `down` for DevDock-managed Compose projects. |
| 3. Readiness and dependency orchestration | Deterministic startup ordering, health/readiness waits, timeouts, and interruption handling. |
| 4. State, recovery, and safe cleanup | Durable operation records and recovery from interrupted or partially failed lifecycle operations. |
| 5. Drift and environment snapshots | Compare declared environment with captured local state and explain meaningful changes. |
| 6. Profiles and runtime adapter expansion | Multiple named profiles and a tested adapter boundary for additional local runtimes. |
| 7. Packaging, compatibility, and release quality | Installable release, supported-platform matrix, polished docs, upgrade path, and release checks. |
| 8. Hardening and usability validation | Fault injection, usability passes, performance checks, security review, and release candidate fixes. |

## Detailed work plan

### 0. Repository and CLI contract

**Status:** Complete. The repository has install/use and contribution docs, a CLI behavior contract, versioned profile rules, explicit path constraints, an MIT license, security guidance, issue/PR templates, a changelog, and commit conventions. CI checks Python 3.12–3.14 on Windows, macOS, and Linux. Automated checks cover the core CLI flow, discovery, profile validation, diagnostics, and package builds; unit checks do not require Docker.

**Acceptance:** A new contributor can create an environment, install the CLI, understand all current commands, run the documented quality checks, and tell which behaviors are implemented versus planned.

### 1. Discovery and diagnostics

**Status:** In progress. Implemented so far: a discovery result with marker evidence, nearest-root selection, nested-root reporting, and filename-only manifest inventory; bounded Python version metadata reads; structured finding fields and stable codes; Python executable/version evidence and a warning when the DevDock interpreter is not the project's `.venv`; separate Docker CLI, daemon, and Compose parser checks; required-file/directory checks with resolved symlink containment; distinct unset/empty environment findings; and loopback port probes. The remaining bullets below are planned work and the acceptance criteria have not yet been met.

- [x] Replace ad hoc project discovery with a tested discovery result describing the selected root and evidence used.
- [x] Detect nested marked roots and report that the nearest one wins plus how to select another project directory.
- [x] Read bounded Python version declarations from `pyproject.toml`, `.python-version`, and `.tool-versions`; inventory other common manifests and lockfiles by filename only. Continue adding safe metadata readers only for fields needed by diagnostics.
- [ ] Distinguish the DevDock interpreter from the target project's configured interpreter and report both executable/version pairs when available.
- [x] Check Docker CLI, Compose plugin, daemon reachability, and Compose file parsing as separate findings.
- [x] Validate profile-configured ports, port collisions on loopback, required files, and expected directories.
- [x] Make environment-variable diagnostics redact values and distinguish unset from empty.
- [x] Introduce a finding model with code, severity, summary, evidence, remediation, and documentation reference.
- Add failure categories and consistent exit-code rules, including warnings and partial checks.
- Add synthetic fixture projects for valid, missing, malformed, nested, ambiguous, unsupported, and platform-specific cases.
- Test command output with paths containing spaces, Unicode, and Windows path separators.

**Acceptance:** Diagnostics are deterministic, actionable, do not expose values, and correctly distinguish an unavailable tool from a malformed project or a failed runtime check.

### 2. Docker Compose adapter and lifecycle

- Define an adapter interface for capability detection, configuration validation, start, status, stop, and resource identification.
- Implement a Compose adapter using argument arrays and subprocess APIs, never shell-string interpolation.
- Define project-scoped resource naming/labels so DevDock can identify what it owns without claiming unrelated containers.
- Implement `devdock up` with bounded execution, captured output, useful error summaries, and an option to show full runtime output.
- Implement `devdock status` with per-service state, health, and relevant published ports.
- Implement `devdock down` as a scoped stop operation with a preview of affected services.
- Decide which Compose options DevDock controls and which user options pass through; reject conflicting or unsafe combinations.
- Preserve useful Compose output while redacting configured secrets and avoiding accidental environment dumps.
- Handle unavailable daemon, invalid Compose config, missing image, failed pull, port conflicts, and startup failures distinctly.
- Add disposable integration fixtures that create only local throwaway resources and clean up in `finally`/fixture teardown.
- Verify repeated `up` and `down` calls are idempotent and do not alter unrelated Compose projects.

**Acceptance:** A sample project can be started, queried, and stopped repeatedly; DevDock reports ownership scope before actions and leaves unrelated resources untouched.

### 3. Readiness and dependency orchestration

- Model service dependencies from Compose definitions and explicit DevDock profile declarations.
- Define readiness sources: Compose health status, TCP connection, HTTP response, and bounded custom checks that do not execute arbitrary untrusted commands by default.
- Specify startup order, parallelism limits, readiness polling interval, per-service timeout, and global deadline.
- Report which dependency is blocking a service and show the last useful health result.
- Handle services with no health check and distinguish “running” from “ready”.
- Define behavior when a dependency becomes unhealthy after startup.
- Support cancellation on Ctrl+C and Windows console interruption; avoid orphaning child processes.
- Ensure timeout and cancellation paths produce a recoverable operation state.
- Test dependency graphs with cycles, missing nodes, diamond dependencies, slow starts, flapping health, and simultaneous failures.
- Test wall-clock bounds with fake adapters and injected clocks rather than slow real sleeps.

**Acceptance:** Startup behavior is predictable for dependency graphs and remains bounded and explainable when a service never becomes ready or the user interrupts the command.

### 4. State, recovery, and safe cleanup

- Decide whether state is stored in a project-local `.devdock/` directory, a user-level directory, or both; document portability and privacy tradeoffs.
- Define a versioned state format and atomic write strategy with corruption detection.
- Record operation IDs, intended action, resource identifiers, timestamps, and terminal/partial status without storing secrets.
- Design state transitions for start requested, resources created, ready, failed, stop requested, stopped, and recovery required.
- Recover after process termination, machine restart, Docker daemon restart, disk-full errors, and state-file corruption.
- Make recovery inspect actual adapter state before attempting a repair; never infer that a resource is safe to remove from a stale name alone.
- Implement `devdock down --dry-run` or an equivalent preview as the default for destructive operations.
- Require an explicit confirmation flag for volume deletion; default stop must preserve data volumes.
- Add safeguards for project identity changes, Compose project-name conflicts, symlink/path surprises, and stale ownership markers.
- Provide a recovery command that explains proposed actions before applying them.
- Fault-inject failure after each lifecycle boundary and verify resulting state can be inspected and recovered.
- Document backup and manual cleanup procedures for state corruption or adapter disagreement.

**Acceptance:** Interrupted operations can be diagnosed and recovered without broad cleanup, accidental volume deletion, or claiming resources belonging to another tool.

### 5. Drift and environment snapshots

- Define which environment facts are safe and useful to snapshot: normalized profile fields, runtime versions, Compose service names, port mappings, and dependency identifiers.
- Explicitly exclude environment values, credentials, container logs, bind-mounted file contents, and personal data.
- Add schema-versioned snapshots with stable ordering and redacted output.
- Implement snapshot comparison that classifies added, removed, and changed fields and links findings to source profile locations.
- Distinguish declared configuration changes from machine-local differences and transient runtime state.
- Decide retention limits and whether old snapshots are automatic, opt-in, or user-triggered.
- Support export/import only if the format remains free of secrets and host-specific private paths.
- Test deterministic output, migration, corrupted snapshot handling, and diffs across supported platforms.
- Document the limits of drift detection; it cannot prove equivalence of images, external services, or files not explicitly inventoried.

**Acceptance:** A user can compare a known snapshot with current declarations and understand exactly what changed without DevDock persisting secret or personal values.

### 6. Profiles and runtime adapter expansion

- Add multiple named profiles for legitimate local scenarios such as minimal services, full stack, and test dependencies.
- Define profile inheritance/overrides carefully; reject ambiguous merge behavior and validate the final resolved profile.
- Add profile selection to inspect/check and later lifecycle commands.
- Stabilize adapter capabilities and versioning so unsupported operations are reported before mutation.
- Choose the next adapter based on user need and maintenance cost; candidates include local Python/Node runtime checks, not cloud provisioning.
- Define adapter conformance tests that every runtime must pass.
- Keep adapter-specific behavior out of CLI command handlers and shared state transitions.
- Specify how adapters expose readiness, ownership, status, cancellation, and cleanup semantics.
- Test missing adapters, incompatible adapter versions, partial capabilities, and runtime changes between commands.
- Document that extension points are not a security sandbox; do not load arbitrary third-party plugins in the initial release.

**Acceptance:** Profiles resolve consistently, and a second adapter can be added without changing the core command and state model or weakening safety guarantees.

### 7. Packaging, compatibility, and release quality

- Choose the supported operating systems and Python version window; test Windows, Linux, and macOS behavior explicitly.
- Validate Windows process termination, paths, quoting, console signals, and Docker Desktop differences.
- Validate Linux Docker socket permissions, rootless Docker, distro package differences, and service availability without assuming systemd.
- Validate macOS Docker Desktop startup delays, file sharing, and path normalization.
- Add a separately gated disposable Docker integration job; never make ordinary unit tests depend on a user's daemon.
- Build and inspect source distributions and wheels; verify entry points, package data, README rendering, and license inclusion.
- Define versioning, changelog format, deprecation policy, configuration migration, and release tagging process.
- Add shell completion only after the command surface stabilizes; cover PowerShell and common POSIX shells.
- Add structured output (such as JSON) with a versioned schema if scripting use cases warrant it.
- Document proxy/network, image pull, registry auth, and offline limitations without storing credentials.
- Add reproducible install instructions and a clean-machine walkthrough from clone through teardown.
- Verify installation and upgrade from the previous supported release with profiles and state present.

**Acceptance:** A release candidate installs cleanly on each supported platform, exposes the documented commands, preserves user data during upgrade, and has reproducible CI evidence.

### 8. Hardening and usability validation

- Conduct a threat review of profile parsing, path handling, subprocess invocation, output redaction, state writes, and cleanup scope.
- Check for command injection, symlink/path traversal surprises, unsafe temporary files, and accidental secret logging.
- Add fuzz/property tests for YAML/profile validation, dependency graphs, state transitions, and snapshot parsing.
- Test offline, slow, flaky, permission-denied, and resource-exhausted conditions.
- Run a usability review with unfamiliar synthetic projects and record where users cannot tell what command to run or what DevDock will change.
- Review all errors for actionable text, stable finding codes, and no sensitive values.
- Check performance with many services and large but reasonable Compose configurations; set documented limits where needed.
- Verify installation, check-only use, startup, interruption, status, teardown, recovery, and upgrade end to end.
- Resolve release-blocking defects, update support policy, and prepare a release checklist with known limitations.

**Acceptance:** A release candidate completes documented workflows on the support matrix, survives the failure scenarios in the plan, and clearly communicates remaining limitations.

## Not planned for the initial product

- A web dashboard or cloud control plane.
- Production or remote infrastructure management.
- Credential vaulting, secret distribution, or `.env` value persistence.
- AI inference or AI features inside DevDock.
- Arbitrary shell hooks from project configuration.
- Automatic deletion of volumes, user files, or unrelated containers.
- Security scanning or exploit-oriented features.

These boundaries keep the first product local, reviewable, and focused on predictable development environments.

