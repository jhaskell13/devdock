# CLI contract, version 0.1

This page describes behavior currently implemented by DevDock. Options may be added in later versions; commands that are not listed here are not implemented yet.

## Commands

| Command | Default directory | Behavior |
| --- | --- | --- |
| `devdock init [DIRECTORY]` | `.` | Creates the directory if needed, then writes a starter `devdock.yaml`. Refuses to overwrite an existing profile. |
| `devdock inspect [DIRECTORY]` | `.` | Finds the nearest project root and reports the profile and common Compose files. If a profile exists, validates it and reports the configured project, runtimes, and the Compose project name DevDock will manage. |
| `devdock up [DIRECTORY]` | `.` | Runs the profile's checks, proves ownership, then starts the project's Compose services detached. Refuses before any mutation if a check fails, the Compose file is missing or escapes the project, Docker is unavailable, or any resource under the project name is not DevDock's. |
| `devdock down [DIRECTORY]` | `.` | Previews then stops and removes the project's DevDock-managed containers and networks. Volumes are never removed. Requires confirmation, `--yes`, or `--dry-run`. |
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
| `compose_file` | Optional forward-slash, project-relative path. Absolute/drive-qualified paths, backslashes, control characters, and lexical `..` traversal are rejected. `check` verifies it exists and that resolved symlinks remain inside the project. |
| `required_env` | Optional list of names accepted as Python identifiers. Each name must be set to a non-empty value in DevDock's inherited process environment. Unset and empty values get distinct findings, and values are never printed. |
| `required_files` | Optional list of project-relative file paths. Absolute paths, drive prefixes, backslashes, control characters, and lexical `..` traversal are rejected. Resolved symlinks must remain inside the project. |
| `required_dirs` | Optional list of project-relative directory paths with the same containment rules as `required_files`. |
| `ports` | Optional distinct integer TCP ports from `1` through `65535`. Each is probed by binding `127.0.0.1`; this is a momentary check, not a reservation. |

Unknown fields are errors. The schema does not load `.env`, expand variables, execute commands, or store state. Python requirements currently accept a minimum such as `>=3.12` or `>=3.12.1`; the check compares major, minor, and patch versions with the interpreter running DevDock. Docker accepts `compose` and separately checks the CLI/plugin, daemon reachability, and Compose config parser. Other runtime names or requirement forms produce a failed finding.

## Output and exit behavior

- `init` prints the created profile path to standard output and exits `0`. If the profile already exists, it writes an error to standard error and exits `1` without replacing the file.
- `inspect` prints the selected root and discovered files to standard output. Invalid profile data is reported on standard error and exits `1`; otherwise it exits `0`.
- `up` prints its result as `[OK]` or `[REFUSED]` with a stable code, the ownership preview, and any redacted runtime output. Successful runs go to standard output and exit `0`; refusals and failures go to standard error and exit `1`.
- `down` prints the plan on standard output, then `[OK] DOWN_STOPPED`, `DOWN_NOTHING_TO_STOP`, or `DOWN_DRY_RUN` with exit `0`; refusals, cancellation, `DOWN_PARTIAL`, and failures go to standard error with exit `1`.
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

### Failure classification

When a start or stop fails, DevDock reads the runtime's message to recognize *which kind* of failure it was, then discards the text. The code, summary, and suggested action are written in DevDock's own words and never quote the output, so classification cannot become a disclosure channel.

| Kind | Codes | Typical cause |
| --- | --- | --- |
| Permission | `UP_PERMISSION_DENIED`, `DOWN_PERMISSION_DENIED` | The user cannot reach the Docker socket. |
| Daemon | `UP_DAEMON_UNAVAILABLE`, `DOWN_DAEMON_UNAVAILABLE` | The daemon stopped responding mid-operation. |
| Configuration | `UP_CONFIG_INVALID` | Compose rejected the file during the run. |
| Registry auth | `UP_REGISTRY_AUTH_REQUIRED` | The registry needs `docker login`. |
| Image | `UP_IMAGE_NOT_FOUND` | The image or tag does not exist. |
| Pull | `UP_PULL_FAILED` | Network, proxy, or rate-limit failure while downloading. |
| Ports | `UP_PORT_CONFLICT` | A published port is already bound. |
| Networks | `UP_NETWORK_CONFLICT` | A network exists or an address pool overlaps. |
| Host resources | `UP_RESOURCE_EXHAUSTED` | Out of disk space, memory, or file handles. |
| In use | `DOWN_RESOURCE_IN_USE` | A network or volume is still attached. |
| Service | `UP_SERVICE_FAILED` | A container exited or a dependency never became healthy. |
| Unrecognized | `UP_FAILED`, `DOWN_FAILED` | DevDock does not recognize the message. |

Every result states the runtime's exit code and whether the message was recognized, so an unrecognized failure is visibly unrecognized rather than silently mislabelled.

### Runtime output and redaction

On success, captured Docker output is not shown. When a failure is *recognized*, its output is also withheld: the code and suggested action already say what to do, and showing the text would risk disclosing values DevDock cannot recognize for no diagnostic gain. When a failure is *unrecognized*, DevDock prints the last 20 meaningful lines after redaction, because that is the only information available. `--show-output` prints all of both streams in either case, also redacted. `--show-output` controls volume, never redaction.

Any value from DevDock's own environment that is at least four characters long is replaced by `${NAME}`, except a short list of well-known non-secret variables such as `PATH`, `TERM`, `LANG`, and `LC_*`; `HOME` and `USER` are treated as private and redacted. Passwords inside `scheme://user:password@host` URLs are replaced with `[redacted]`. Values are matched in one pass, longest first, so a short value cannot rewrite another value's placeholder.

A second layer matches credentials by shape rather than by value, so it catches secrets DevDock has never seen — including values Compose read from the project's `.env`. It covers private key blocks, JWTs, AWS key IDs, GitHub and Slack tokens, `sk-` style API keys, `Bearer` headers, and `key=value` pairs whose key names a secret such as `POSTGRES_PASSWORD` or `api_key`. The key is kept and the value replaced, so the message still explains what went wrong.

Neither layer is a guarantee. A secret with no recognizable shape, not present in DevDock's environment, and not in a named assignment can still appear in output. Redaction also scans at most 256 KB; beyond that the output is truncated first.

This is a mitigation, not a guarantee. Compose also reads the project's `.env` file, which DevDock never opens, so a value that exists only there cannot be recognized and could appear in output. Treat `--show-output` as a deliberate disclosure and review what you paste elsewhere.

## Stopping services

`devdock down` is the only destructive command. It always prints a plan first:

- containers it will stop and remove, by name, service, and state;
- project networks it will remove;
- DevDock containers it will leave in place because their service is no longer declared in the current Compose file;
- volumes it will preserve. No DevDock command removes volumes in this version; that is later roadmap work with its own authorization design.

The plan is built from the same gates as `up` except the profile's diagnostics, which are deliberately skipped so a project can be torn down even when, say, a required environment variable is unset. Any ownership conflict, incomplete inventory, or unreadable Compose file refuses before the plan is shown.

Then one of three things happens. `--dry-run` prints `DOWN_DRY_RUN` and exits `0` without changing anything. Otherwise, if there is nothing to stop, DevDock reports `DOWN_NOTHING_TO_STOP` and runs no command; repeating `down` is safe. If there is work to do, DevDock asks for confirmation on an interactive terminal, or requires `--yes` when standard input is not a terminal, and refuses with `DOWN_CONFIRMATION_REQUIRED` otherwise. Declining prints `DOWN_CANCELLED` and exits `1` with nothing changed.

Immediately before acting, DevDock re-reads the inventory. If the set of managed containers changed while the prompt was open, or a conflict appeared, it refuses with `DOWN_PLAN_STALE` and nothing is run; rerun `down` to see the current plan.

The command run is `docker compose --project-directory <root> --file <compose file> --project-name <derived name> down --remove-orphans=false`, with no `--volumes` or `--rmi`. After it returns DevDock re-inventories the project: `DOWN_STOPPED` means the re-inventory succeeded and no planned container or network survived; `DOWN_PARTIAL` is returned, with exit `1`, when something remains or when the re-inventory itself failed, so DevDock never claims success it could not verify. A timeout reports that some resources may already be gone and points at `devdock status`. Runtime output is handled exactly as for `up`.

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

### Environment handed to Compose

DevDock runs Compose with its own environment plus three pinned settings: `COMPOSE_REMOVE_ORPHANS=false`, `COMPOSE_ANSI=never`, and `COMPOSE_PROGRESS=plain`. Compose reads `COMPOSE_*` settings from the project's `.env`, which DevDock treats as untrusted; shell values take precedence over `.env` in Compose, so these pins keep the `down` preview truthful and the captured output free of colour codes whatever `.env` says. Other `.env` content, including `DOCKER_HOST` or `DOCKER_CONTEXT`, is honoured by Compose exactly as it would be if you ran `docker compose` in that directory yourself; DevDock does not read the file to check.

## Ownership scope

Lifecycle commands (planned) act only on Compose resources DevDock can prove it created. `inspect` already prints the Compose project name DevDock derives for a profile so the resources are recognizable in `docker ps`.

- The project name is `devdock-<slug>-<digest>`. `<slug>` is the profile's `project` lowercased with every run of non-alphanumeric characters replaced by one hyphen, trimmed, and limited to 40 characters (`project` if nothing remains). `<digest>` is the first 12 hex characters of the SHA-256 of the resolved project root path, case-folded on Windows. The same project at the same directory always maps to the same name; a copy elsewhere gets a different one; symlinked paths to one directory share one name.
- Before acting, DevDock lists containers under that name (by Compose project label and by anchored name prefix), plus labeled networks and volumes. A container counts as managed only if Compose's own labels show it was created from this project root, from exactly this Compose file, as a service rather than a one-off `compose run`.
- Any container under the name that fails those checks, any unparseable runtime output, or any failed inventory query makes DevDock refuse the action and print why, with a `docker ps` command the user can run to inspect the resources. DevDock never adopts resources on the strength of a familiar name.
- Changing `compose_file` or moving the project while resources exist is reported as a mismatch rather than silently creating a parallel project; stop the old resources with the configuration that created them first.
- Volumes under the name are listed as preserved. No DevDock command removes volumes in this version.

Only identity fields and the ownership labels above are requested from Docker, each JSON-encoded by the template so a tab or newline inside a label cannot forge an extra record; container environments, images, and full label sets are never read for this purpose.

### Limits of label-based ownership

This model protects against accidental overlap: another project, a renamed directory, a stale configuration, or a one-off container. It is not a defence against a hostile local process with access to the Docker socket. Such a process can set exactly the labels DevDock looks for and make its container count as managed, and it can already control everything Docker manages. Networks and volumes are matched by the project label alone because Compose records no working-directory or file labels on them. Durable records of what DevDock itself created are later roadmap work and will tighten these rules.

## Diagnostic codes

Each `check` result includes a stable code, severity, summary, optional evidence, suggested action, and documentation reference. Scripts should use codes only after the CLI adds its planned versioned machine-readable format; the current human-readable output is not a scripting API.

| Code family | Examples | Meaning |
| --- | --- | --- |
| Profile/runtime | `PROFILE_VALID`, `PYTHON_VERSION_OK`, `PYTHON_VERSION_TOO_OLD`, `RUNTIME_UNSUPPORTED` | Profile validity and declared runtime support. Python evidence identifies the exact DevDock interpreter. |
| Docker/Compose | `DOCKER_CLI_MISSING`, `COMPOSE_CLI_UNAVAILABLE`, `DOCKER_DAEMON_UNAVAILABLE`, `COMPOSE_CONFIG_INVALID` | Separate CLI/plugin, daemon, and config parser outcomes. Parser output is suppressed to protect interpolated values. |
| Paths | `REQUIRED_FILE_FOUND`, `REQUIRED_FILE_MISSING`, `REQUIRED_DIRECTORY_OUTSIDE_PROJECT` | Required filesystem entries and resolved project containment. |
| Environment | `ENVIRONMENT_VARIABLE_UNSET`, `ENVIRONMENT_VARIABLE_EMPTY`, `ENVIRONMENT_VARIABLE_SET` | Presence state only; values are not included in findings. |
| Ports | `PORT_AVAILABLE`, `PORT_UNAVAILABLE` | A momentary TCP bind probe on loopback. |
| Lifecycle failures | `UP_PORT_CONFLICT`, `UP_IMAGE_NOT_FOUND`, `UP_REGISTRY_AUTH_REQUIRED`, `UP_PULL_FAILED`, `UP_CONFIG_INVALID`, `UP_SERVICE_FAILED`, `UP_RESOURCE_EXHAUSTED`, `DOWN_RESOURCE_IN_USE` | The recognized kind of a failed start or stop. Output is withheld because the code already explains it. |
| Lifecycle | `UP_STARTED`, `UP_PREFLIGHT_FAILED`, `UP_OWNERSHIP_CONFLICT`, `UP_TIMED_OUT`, `UP_FAILED`, `STATUS_REPORTED`, `STATUS_INCOMPLETE`, `DOWN_STOPPED`, `DOWN_PARTIAL`, `DOWN_PLAN_STALE`, `DOWN_CONFIRMATION_REQUIRED`, `DOWN_OWNERSHIP_CONFLICT` | Outcome of a lifecycle command; refusals name the gate that stopped it. |
| Partial checks | `DOCKER_DAEMON_SKIPPED`, `COMPOSE_CONFIG_SKIPPED` | A prerequisite prevented a dependent check from running; evidence states why it was skipped. |

Codes are illustrative and not exhaustive; the `code` field in source is authoritative. Existing codes should not be repurposed for different failure meanings.

