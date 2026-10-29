# Initial architecture

DevDock starts as a Python CLI for inspecting and checking one local project. The first increment keeps commands read-only except for `init`, which creates the target directory when needed and writes a profile file.

## Runtime flow

```text
Typer commands
  ├─ init      writes a starter profile
  ├─ inspect   discovers the project root and reports known files
  └─ check     loads the profile, validates it, and runs diagnostics

Profile loader (Pydantic + safe YAML parsing)
  └─ ProjectProfile, schema version 1

Runtime adapter (adapters/)
  ├─ RuntimeAdapter protocol: capability detection, config validation, resource inventory
  ├─ ComposeAdapter: argument arrays, bounded timeouts, injectable CommandRunner
  └─ results carry status only; captured output is never copied into findings

Ownership (ownership.py)
  ├─ deterministic Compose project name: devdock-<slug>-<root digest>
  ├─ classifies inventoried containers against expected Compose labels
  └─ assessment is safe only with zero conflicts and zero inventory errors

Diagnostics
  ├─ structured findings with stable code, severity, evidence, and remediation
  ├─ declared Python minimum and exact DevDock interpreter
  ├─ Docker CLI/plugin, daemon, and Compose parsing as separate checks
  ├─ configured and required project paths with symlink containment
  ├─ required environment names without exposing values
  └─ loopback TCP port availability probes
```

`adapters/base.py` defines the adapter contract and status-only result types; `adapters/compose.py` is the only module that locates the Docker executable and runs it. `discovery.py` locates the nearest ancestor with a DevDock profile, common Compose filename, or supported project manifest and returns the evidence, marked ancestors, and filename-only inventory. `metadata.py` reads bounded, project-contained Python version declarations from `pyproject.toml`, `.python-version`, and `.tool-versions`; it does not execute project code or read environment templates. `config.py` owns the profile schema and parsing errors. `diagnostics.py` returns structured findings; the CLI renders them and chooses the process exit code. Runtime output that may contain interpolated values is not included in findings.

## Profile contract, version 1

The root YAML object has these fields:

- `schema_version`: required integer, currently `1`.
- `project`: required non-empty project name.
- `runtimes`: optional mapping of runtime identifiers to requirements. The first checks support `python: ">=X.Y"` and `docker: "compose"`.
- `compose_file`: optional project-relative Compose file path. Absolute paths, drive-qualified paths, backslashes, and lexical `..` traversal are rejected. Symlink-aware containment remains future work before this path is used for mutations.
- `required_env`: optional list of environment variable names. DevDock checks whether each name is set in its process environment and never prints values.
- `required_files` and `required_dirs`: optional project-relative filesystem paths. Lexical traversal and resolved symlinks outside the project are rejected by checks.
- `ports`: optional distinct TCP ports to probe on loopback; results are momentary observations.

Unknown fields are rejected so typos do not silently weaken a check. Profile values contain configuration only; credentials remain in the user's local environment.

## Adapter boundary

All Docker invocation lives behind `RuntimeAdapter`. The Compose implementation builds argument arrays, never shell strings, runs each command with an explicit timeout, and returns `ComposeCapabilities` and `ConfigValidation` values that contain exit status and executable paths but no captured output. Diagnostics and later command handlers consume those values; tests inject a fake `CommandRunner` instead of patching module globals. Lifecycle methods (start, status, stop, resource identification) are added to the protocol by the milestone that implements them, so the contract never advertises unimplemented behavior. `ownership.py` holds the pure rules for what DevDock owns: the derived project name and the label values Compose writes when DevDock runs it with an absolute `--project-directory` and `-f`. The adapter's inventory returns only identity fields and those labels. Lifecycle commands print the assessment before acting and refuse when any resource under the name is not provably theirs. Destructive cleanup and volume removal require a separate preview/authorization design.

SQLite is deferred until the product needs durable snapshots or recovery state. The first milestone has no daemon, web UI, cloud connection, credential store, or AI features.

