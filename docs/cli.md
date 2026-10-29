# CLI contract, version 0.1

This page describes behavior currently implemented by DevDock. Options may be added in later versions; commands that are not listed here are not implemented yet.

## Commands

| Command | Default directory | Behavior |
| --- | --- | --- |
| `devdock init [DIRECTORY]` | `.` | Creates the directory if needed, then writes a starter `devdock.yaml`. Refuses to overwrite an existing profile. |
| `devdock inspect [DIRECTORY]` | `.` | Finds the nearest project root and reports the profile and common Compose files. If a profile exists, validates it and reports the configured project, runtimes, and the Compose project name DevDock will manage. |
| `devdock up [DIRECTORY]` | `.` | Runs the profile's checks, proves ownership, then starts the project's Compose services detached. Refuses before any mutation if a check fails, the Compose file is missing or escapes the project, Docker is unavailable, or any resource under the project name is not DevDock's. |
| `devdock status [DIRECTORY]` | `.` | Reports each declared service's container, state, Compose health, readiness class, and published ports. Read-only; ownership conflicts are listed, not treated as a refusal. |
| `devdock check [DIRECTORY]` | `.` | Finds the nearest project root, requires `devdock.yaml`, validates it, then runs the checks declared by that profile. |
| `devdock --help` | — | Lists commands and options. Each command also accepts `--help`. |

`init` uses exactly the directory supplied; it does not search parent directories. `inspect` and `check` start at the supplied directory, resolve it to an absolute path, then walk upward. The first directory containing a DevDock profile, common Compose file, `pyproject.toml`, `package.json`, `Cargo.toml`, or `go.mod` is selected. If no marker exists, the supplied directory is used as the root. When multiple marker types exist at the same level, that directory is selected. `inspect` prints the marker that selected the root and inventories known project manifests and lockfiles by filename. It reads only recognized Python version metadata from `pyproject.toml`, `.python-version`, and `.tool-versions`; it never reads `.env` or environment-template file contents.

If a project marker also exists above the selected root, `inspect` reports nested roots and explains that the nearest root wins. To select another nested project, pass a directory inside that project. The inventory recognizes common Compose names, Python metadata and lockfiles, environment templates, and Node, Rust, and Go manifests; this list is descriptive and does not imply runtime support.

`inspect` succeeds if no profile is found; it reports that the profile is missing. It fails if a discovered profile is invalid. `check` requires a valid profile, even if a Compose file was what caused discovery to select that root.

## Profile validation

The profile is UTF-8 YAML loaded with safe parsing. The root must be an object with these fields:

| Field | Contract |
| --- | --- |
| `schema_version` | Required integer `1`; any other version is rejected. |
| `project` | Required, non-empty string; whitespace-only values are rejected. |
| `runtimes` | Optional mapping of lowercase runtime names to non-empty requirement strings. Python checks use the interpreter running DevDock and print its executable and version. If that executable resolves to this project’s `.venv`, `venv`, or `env`, DevDock identifies it as the target interpreter too; DevDock never launches a project-controlled interpreter to probe its version. |
| `compose_file` | Optional forward-slash, project-relative path. Absolute/drive-qualified paths, backslashes, and lexical `..` traversal are rejected. `check` verifies it exists and that resolved symlinks remain inside the project. |
| `required_env` | Optional list of names accepted as Python identifiers. Each name must be set to a non-empty value in DevDock's inherited process environment. Unset and empty values get distinct findings, and values are never printed. |
| `required_files` | Optional list of project-relative file paths. Absolute paths, drive prefixes, backslashes, NUL, and lexical `..` traversal are rejected. Resolved symlinks must remain inside the project. |
| `required_dirs` | Optional list of project-relative directory paths with the same containment rules as `required_files`. |
| `ports` | Optional distinct integer TCP ports from `1` through `65535`. Each is probed by binding `127.0.0.1`; this is a momentary check, not a reservation. |

Unknown fields are errors. The schema does not load `.env`, expand variables, execute commands, or store state. Python requirements currently accept a minimum such as `>=3.12` or `>=3.12.1`; the check compares major, minor, and patch versions with the interpreter running DevDock. Docker accepts `compose` and separately checks the CLI/plugin, daemon reachability, and Compose config parser. Other runtime names or requirement forms produce a failed finding.

## Output and exit behavior

- `init` prints the created profile path to standard output and exits `0`. If the profile already exists, it writes an error to standard error and exits `1` without replacing the file.
- `inspect` prints the selected root and discovered files to standard output. Invalid profile data is reported on standard error and exits `1`; otherwise it exits `0`.
- `up` prints its result as `[OK]` or `[REFUSED]` with a stable code, the ownership preview, and any redacted runtime output. Successful runs go to standard output and exit `0`; refusals and failures go to standard error and exit `1`.
- `status` prints `[OK] STATUS_REPORTED` followed by the table on standard output and exits `0`, or `STATUS_INCOMPLETE`/a refusal code on standard error with exit `1`.
- `check` prints each finding to standard output as `[PASS]`, `[WARN]`, `[SKIP]`, or `[FAIL]` followed by a stable code and summary, then optional evidence and a suggested action. Warnings and skipped checks do not fail the command. It exits `0` if there are no failed findings and `1` if the profile is missing/invalid or any check fails.
- Typer handles command-line syntax and argument errors; those use its parser exit behavior.

The output is intended for people. There is no machine-readable output format yet, so scripts should not depend on exact prose or formatting.

## Current check boundaries

`check` is read-only. It checks the Python interpreter running DevDock when declared. When DevDock is already running from this project's `.venv`, `venv`, or `env`, it reports that same executable as the target interpreter; it never starts an arbitrary project interpreter. Otherwise, it reports the DevDock executable/version and warns that the target was not inspected. Docker diagnostics separately check CLI/plugin response, daemon reachability, Compose file presence, and Compose config parsing. Docker command output is suppressed; findings use only return status and safe summaries. Compose files and required paths resolving outside the project fail before their contents are consumed. Required paths are checked without reading file contents. Environment diagnostics report unset versus empty without displaying values. Port checks probe loopback availability and can become stale immediately. Checks do not start services, wait for service health, or prove that the project can run. See [`ROADMAP.md`](../ROADMAP.md) for later lifecycle work.

## Starting services

`devdock up` is the only command that mutates anything outside the profile file. It runs these gates in order and stops at the first failure, so nothing is started unless all of them pass:

1. `check` diagnostics for the profile; any `FAIL` refuses the start.
2. A Compose file must be configured or discoverable, must exist, and must resolve inside the project.
3. The Docker Compose CLI must respond and the daemon must be reachable.
4. The ownership assessment must be clean. Conflicts and incomplete inventories both refuse.

DevDock then runs `docker compose --project-directory <root> --file <compose file> --project-name <derived name> up --detach`. It supplies those options itself and forwards no user-provided Compose options in this version. It does not pass `--wait`, `--remove-orphans`, or `--force-recreate`.

`up` returns when Compose has created and started the containers. That is not the same as ready: a container can be running while the service inside it is still starting. DevDock says so on success and does not poll for health. Readiness and dependency ordering are Milestone 3.

`--timeout SECONDS` bounds the run and defaults to 300. On timeout DevDock reports that containers may already have been created and points at the `docker ps` command for the project, because a timed-out start is not the same as a start that did nothing.

Repeating `up` with resources already running is safe; Compose reconciles to the same state and the ownership gate still passes because the resources are DevDock's.

### Runtime output and redaction

Captured Docker output is hidden by default. On failure DevDock prints the last 20 meaningful lines of it; `--show-output` prints all of both streams.

Both paths are redacted and `--show-output` does not disable redaction. Any value from DevDock's own environment that is at least four characters long is replaced by `${NAME}`, and passwords inside `scheme://user:password@host` URLs are replaced with `[redacted]`.

This is a mitigation, not a guarantee. Compose also reads the project's `.env` file, which DevDock never opens, so a value that exists only there cannot be recognized and could appear in output. Treat `--show-output` as a deliberate disclosure and review what you paste elsewhere.

## Reporting status

`devdock status` is read-only. It needs a Compose file inside the project and a responsive Docker daemon; it does not require the profile's checks to pass, because a status report is most useful when something is wrong.

It combines three sources: the ownership inventory (which containers are provably DevDock's), `docker compose config --services` (which services the current Compose file declares; names only, no rendered values), and `docker compose ps --all --format json` (state, health, exit code, published ports). From the runtime report DevDock keeps only ID, name, service, state, health, exit code, and publishers; image, command, and labels are discarded at parse time.

Each row carries a readiness class. These are the vocabulary later readiness work builds on:

| Readiness | Meaning |
| --- | --- |
| `ready` | Running and the Compose health check passes. |
| `running (readiness unverified)` | Running with no health check. DevDock can show the container is alive, not that the service is serving. |
| `starting` | Running; the health check has not passed yet. |
| `unhealthy` | Running; the health check is failing. |
| `stopped` | The container exists but is not running. The exit code is shown when known. |
| `missing` | Declared in the Compose file; no DevDock-managed container exists. |
| `unknown` | The runtime reported a state DevDock does not model. |

Services are listed in declared order, one row per container when a service has replicas, followed by any DevDock-managed containers no longer declared in the current Compose file. Runtime-reported containers DevDock cannot prove it created are ignored and counted in a note. Resources under the project name that fail the ownership rules are listed separately as not managed.

Exit code is `0` when the report is complete and `1` when a required input is missing or any runtime query failed; a partial table is still printed in that case. Readiness does not affect the exit code yet; scripts should wait for the planned machine-readable output rather than parse this table.

## Ownership scope

Lifecycle commands (planned) act only on Compose resources DevDock can prove it created. `inspect` already prints the Compose project name DevDock derives for a profile so the resources are recognizable in `docker ps`.

- The project name is `devdock-<slug>-<digest>`. `<slug>` is the profile's `project` lowercased with every run of non-alphanumeric characters replaced by one hyphen, trimmed, and limited to 40 characters (`project` if nothing remains). `<digest>` is the first 12 hex characters of the SHA-256 of the resolved project root path, case-folded on Windows. The same project at the same directory always maps to the same name; a copy elsewhere gets a different one; symlinked paths to one directory share one name.
- Before acting, DevDock lists containers under that name (by Compose project label and by anchored name prefix), plus labeled networks and volumes. A container counts as managed only if Compose's own labels show it was created from this project root, from exactly this Compose file, as a service rather than a one-off `compose run`.
- Any container under the name that fails those checks, any unparseable runtime output, or any failed inventory query makes DevDock refuse the action and print why, with a `docker ps` command the user can run to inspect the resources. DevDock never adopts resources on the strength of a familiar name.
- Changing `compose_file` or moving the project while resources exist is reported as a mismatch rather than silently creating a parallel project; stop the old resources with the configuration that created them first.
- Volumes under the name are listed as preserved. No DevDock command removes volumes in this version.

Only identity fields and the ownership labels above are requested from Docker; container environments, images, and full label sets are never read for this purpose.

## Diagnostic codes

Each `check` result includes a stable code, severity, summary, optional evidence, suggested action, and documentation reference. Scripts should use codes only after the CLI adds its planned versioned machine-readable format; the current human-readable output is not a scripting API.

| Code family | Examples | Meaning |
| --- | --- | --- |
| Profile/runtime | `PROFILE_VALID`, `PYTHON_VERSION_OK`, `PYTHON_VERSION_TOO_OLD`, `RUNTIME_UNSUPPORTED` | Profile validity and declared runtime support. Python evidence identifies the exact DevDock interpreter. |
| Docker/Compose | `DOCKER_CLI_MISSING`, `COMPOSE_CLI_UNAVAILABLE`, `DOCKER_DAEMON_UNAVAILABLE`, `COMPOSE_CONFIG_INVALID` | Separate CLI/plugin, daemon, and config parser outcomes. Parser output is suppressed to protect interpolated values. |
| Paths | `REQUIRED_FILE_FOUND`, `REQUIRED_FILE_MISSING`, `REQUIRED_DIRECTORY_OUTSIDE_PROJECT` | Required filesystem entries and resolved project containment. |
| Environment | `ENVIRONMENT_VARIABLE_UNSET`, `ENVIRONMENT_VARIABLE_EMPTY`, `ENVIRONMENT_VARIABLE_SET` | Presence state only; values are not included in findings. |
| Ports | `PORT_AVAILABLE`, `PORT_UNAVAILABLE` | A momentary TCP bind probe on loopback. |
| Lifecycle | `UP_STARTED`, `UP_PREFLIGHT_FAILED`, `UP_OWNERSHIP_CONFLICT`, `UP_TIMED_OUT`, `UP_FAILED`, `STATUS_REPORTED`, `STATUS_INCOMPLETE`, `STATUS_DAEMON_UNAVAILABLE` | Outcome of a lifecycle command; refusals name the gate that stopped it. |
| Partial checks | `DOCKER_DAEMON_SKIPPED`, `COMPOSE_CONFIG_SKIPPED` | A prerequisite prevented a dependent check from running; evidence states why it was skipped. |

Codes are illustrative and not exhaustive; the `code` field in source is authoritative. Existing codes should not be repurposed for different failure meanings.

