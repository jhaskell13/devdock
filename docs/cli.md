# CLI contract, version 0.1

This page describes behavior currently implemented by DevDock. Options may be added in later versions; commands that are not listed here are not implemented yet.

## Commands

| Command | Default directory | Behavior |
| --- | --- | --- |
| `devdock init [DIRECTORY]` | `.` | Creates the directory if needed, then writes a starter `devdock.yaml`. Refuses to overwrite an existing profile. |
| `devdock inspect [DIRECTORY]` | `.` | Finds the nearest project root and reports the profile and common Compose files. If a profile exists, validates it and reports the configured project and runtimes. |
| `devdock check [DIRECTORY]` | `.` | Finds the nearest project root, requires `devdock.yaml`, validates it, then runs the checks declared by that profile. |
| `devdock --help` | — | Lists commands and options. Each command also accepts `--help`. |

`init` uses exactly the directory supplied; it does not search parent directories. `inspect` and `check` start at the supplied directory, resolve it to an absolute path, then walk upward. The first directory containing `devdock.yaml` or one of `compose.yaml`, `compose.yml`, `docker-compose.yaml`, or `docker-compose.yml` is selected. If no marker exists, the supplied directory is used as the root. When a profile and Compose file are both present at the same level, that directory is selected. `inspect` prints the marker that selected the root and inventories known project manifests and lockfiles by filename only. It never reads `.env` or other secret-bearing files for inventory.

If a project marker also exists above the selected root, `inspect` reports nested roots and explains that the nearest root wins. To select another nested project, pass a directory inside that project. The inventory recognizes common Compose names, Python metadata and lockfiles, environment templates, and Node, Rust, and Go manifests; this list is descriptive and does not imply runtime support.

`inspect` succeeds if no profile is found; it reports that the profile is missing. It fails if a discovered profile is invalid. `check` requires a valid profile, even if a Compose file was what caused discovery to select that root.

## Profile validation

The profile is UTF-8 YAML loaded with safe parsing. The root must be an object with these fields:

| Field | Contract |
| --- | --- |
| `schema_version` | Required integer `1`; any other version is rejected. |
| `project` | Required, non-empty string; whitespace-only values are rejected. |
| `runtimes` | Optional mapping of lowercase runtime names to non-empty requirement strings. |
| `compose_file` | Optional forward-slash, project-relative path. Absolute/drive-qualified paths, backslashes, and lexical `..` traversal are rejected. If provided, `check` verifies the file exists. |
| `required_env` | Optional list of names accepted as Python identifiers. Each name must be set to a non-empty value in DevDock's inherited process environment. |

Unknown fields are errors. The schema does not load `.env`, expand variables, execute commands, or store state. Python requirements currently accept a simple minimum form such as `>=3.12`; the check compares it with the interpreter running DevDock. Docker accepts `compose` and verifies that `docker compose version` succeeds. Other runtime names or requirement forms produce a failed finding.

## Output and exit behavior

- `init` prints the created profile path to standard output and exits `0`. If the profile already exists, it writes an error to standard error and exits `1` without replacing the file.
- `inspect` prints the selected root and discovered files to standard output. Invalid profile data is reported on standard error and exits `1`; otherwise it exits `0`.
- `check` prints each finding to standard output as `[PASS]` or `[FAIL]`. A failed finding may include a suggested action. It exits `0` if every declared check passes and `1` if the profile is missing/invalid or any check fails.
- Typer handles command-line syntax and argument errors; those use its parser exit behavior.

The output is intended for people. There is no machine-readable output format yet, so scripts should not depend on exact prose or formatting.

## Current check boundaries

`check` is read-only. It checks the running Python version when declared, Docker CLI/Compose availability when declared, the configured Compose file path, and required environment variable names. It does not query whether the Docker daemon is running, parse Compose configuration, inspect ports, start services, wait for health, or prove that the project can run. Those behaviors are planned in [`ROADMAP.md`](../ROADMAP.md).

