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

Diagnostics
  ├─ declared Python minimum
  ├─ Docker Compose CLI availability
  ├─ configured Compose file
  └─ required environment variable names
```

`discovery.py` locates the nearest ancestor with `devdock.yaml` or a common Compose filename. `config.py` owns the profile schema and parsing errors. `diagnostics.py` returns structured findings; the CLI is responsible for rendering them and choosing the process exit code.

## Profile contract, version 1

The root YAML object has these fields:

- `schema_version`: required integer, currently `1`.
- `project`: required non-empty project name.
- `runtimes`: optional mapping of runtime identifiers to requirements. The first checks support `python: ">=X.Y"` and `docker: "compose"`.
- `compose_file`: optional project-relative Compose file path. Absolute paths, drive-qualified paths, backslashes, and lexical `..` traversal are rejected. Symlink-aware containment remains future work before this path is used for mutations.
- `required_env`: optional list of environment variable names. DevDock checks whether each name is set in its process environment and never prints values.

Unknown fields are rejected so typos do not silently weaken a check. Profile values contain configuration only; credentials remain in the user's local environment.

## Adapter boundary

The initial diagnostics call the local Docker CLI directly and do not manage containers. Before adding `up`, `status`, and `down`, isolate Compose operations behind a runtime adapter with explicit inspect/start/status/stop methods. Lifecycle state should identify only resources DevDock created. Destructive cleanup and volume removal require a separate preview/authorization design.

SQLite is deferred until the product needs durable snapshots or recovery state. The first milestone has no daemon, web UI, cloud connection, credential store, or AI features.

