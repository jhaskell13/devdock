# Initial architecture

DevDock is a Python CLI for inspecting, checking, starting, and stopping one local project's Docker Compose services. `inspect`, `check`, and `status` are read-only. `init` writes one profile file. `up` and `down` mutate only Compose resources DevDock can prove it created, after printing what they will touch.

## Runtime flow

```text
Typer commands
  ├─ init      writes a starter profile
  ├─ inspect   discovers the project root and reports known files
  ├─ check     loads the profile, validates it, and runs diagnostics
  ├─ up        gates on checks and ownership, then starts services detached
  ├─ status    joins declared services, owned containers, and runtime state
  └─ down      plans, confirms, stops containers and networks, verifies; never volumes

Profile loader (Pydantic + safe YAML parsing)
  └─ ProjectProfile, schema version 1

Runtime adapter (adapters/)
  ├─ RuntimeAdapter protocol: capability detection, config validation, resource inventory
  ├─ ComposeAdapter: argument arrays, bounded timeouts, injectable CommandRunner
  └─ results carry status only; captured output is never copied into findings

Lifecycle (lifecycle.py)
  ├─ policy only: every gate runs before the adapter is asked to mutate
  ├─ down is split into plan_stop (preview) and execute_stop (re-check, act, verify)
  └─ returns redacted display lines and a stable code

Status (status.py)
  ├─ readiness classes: ready, running, starting, unhealthy, stopped, missing, unknown
  ├─ classify_readiness(state, health) is the single source of that mapping
  └─ joins declared order, managed containers, and observations; ignores unproven ones

Readiness probes (probes.py)
  ├─ Network performs; run_probe decides. Tests inject a fake and open no socket
  ├─ host re-checked, resolved, every address proven loopback, then that address contacted
  ├─ HTTP: no proxy, no redirects, no cookies, no credentials, body never read
  └─ results use a closed vocabulary defined in the module; endpoint text is never repeated

Dependency listing (adapters/compose.py)
  ├─ `docker compose config --format json --no-interpolate`
  ├─ the flag stops ${VAR}, env_file, and .env values being rendered into the output
  └─ keeps service names, depends_on edges, and health declarations; discards the rest

Dependency graph (graph.py)
  ├─ merges Compose-declared and profile-declared dependencies; both are honored
  ├─ rejects undeclared services, contradictory conditions, unmeetable health waits, cycles
  ├─ reports every problem in one pass; one representative path per cyclic group
  └─ returns sorted startup layers; pure functions over plain mappings

Failures (failures.py)
  ├─ literal-substring markers map runtime prose to a stable kind
  └─ returns only DevDock's own code, summary, and remedy; never quotes output

Redaction (redaction.py)
  ├─ known environment values, then credential shapes, then URL passwords
  └─ all quantifiers bounded; input capped before scanning

Ownership (ownership.py)
  ├─ deterministic Compose project name: devdock-<slug>-<root digest>
  ├─ classifies inventoried containers against expected Compose labels
  └─ assessment is safe only with zero conflicts and zero inventory errors

Diagnostics
  ├─ structured findings with stable code, severity, evidence, and remediation
  ├─ declared Python minimum and exact DevDock interpreter
  ├─ Docker CLI/plugin, daemon (via `docker version`, never `docker info`), and Compose parsing
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
- `compose_file`: optional project-relative Compose file path. Absolute paths, drive-qualified paths, backslashes, and lexical `..` traversal are rejected. `check`, `up`, `status`, and `down` all verify that the resolved path stays inside the project before it is used.
- `required_env`: optional list of environment variable names. DevDock checks whether each name is set in its process environment and never prints values.
- `required_files` and `required_dirs`: optional project-relative filesystem paths. Lexical traversal and resolved symlinks outside the project are rejected by checks.
- `ports`: optional distinct TCP ports to probe on loopback; results are momentary observations.
- `services`: optional per-service `depends_on` conditions and `readiness` probes. Validated on load and not yet consumed by any command. Service names are re-validated against Compose's own shape so a name can never be read as a command-line option. Probes are `tcp` or `http` only, bounded by an explicit timeout, and restricted to loopback at load time and again before connecting; a profile is data and must not be able to direct DevDock at another host or run a command.

Unknown fields are rejected so typos do not silently weaken a check. Profile values contain configuration only; credentials remain in the user's local environment.

## Readiness foundation

`status.py` separates *running* from *ready*. Compose health is the only readiness source DevDock currently evaluates and is recorded per row as `readiness_source`, so the TCP and HTTP probes the profile can now declare, and dependency blocking, can be added as new sources without changing the classes or the renderer. `ProjectStatus.all_ready` is informational now and is the predicate a future `--wait` will poll.

The profile's `services` block is the declaration half of that work: `config.py` owns the dependency conditions and probe definitions, their safety rules, and their defaults. Keeping the schema ahead of the engine means the format's constraints — loopback-only probes, no command probes, service names that cannot be mistaken for options — are settled and tested before any code acts on them.

### Probing without becoming a way out of the machine

`probes.py` is the only module that opens a network connection, and the target comes from a profile that may have arrived with a cloned repository. It is split the same way commands are: `Network` performs the socket work and `run_probe` decides what the outcome means, so the policy is tested against a fake and the test suite opens no socket except the loopback listeners it starts in-process.

The loopback rule is enforced twice, for two different reasons. `config.py` rejects a non-loopback host when the profile loads, which stops a profile naming another machine. `probes.py` re-checks it, resolves it, and requires *every* resolved address to be loopback, which stops a name that is loopback-shaped but resolves elsewhere — something `localhost` can do depending on how a host is configured. DevDock then connects to the address it just validated instead of resolving again, so there is no window between the check and the connection. The declared host still travels as the `Host` header.

The HTTP opener is built by listing handlers rather than taking `build_opener`'s defaults, which include file, FTP, data, and HTTPS handlers this has no use for. `ProxyHandler({})` is not a default and is the reason a `http_proxy` variable in the environment cannot route a loopback probe through a proxy. Redirects are reported as their status rather than followed, because following one hands the endpoint the choice of DevDock's next destination. The response body is never read at all.

Every result carries a phrase defined in that module, so the whole vocabulary is reviewable in one place and no endpoint text can reach a terminal. One distinction is worth the code it costs: a connection that is accepted and then dropped is reported as such rather than as an unreachable host, because that is precisely what a service does between binding its port and being able to serve — the window a readiness probe exists to watch. It arrives as two different exceptions depending on whether the service hung up before or after reading the request, and both are mapped to the same phrase.

### Reading dependencies without reading secrets

`ComposeAdapter.list_dependencies` is the only place DevDock asks Compose for the whole project document, because `depends_on` is the one ordering fact no narrower query exposes, and because Compose is the only thing that resolves `extends`, `include`, overrides, and profiles correctly. Parsing the YAML in DevDock would be safer with respect to disclosure and produce a confidently wrong graph.

That document is dangerous by default. Verified against a real Compose: `docker compose config --format json` expands `${VAR}` from the shell *and* from the project's `.env`, and resolves every `env_file` into each service's `environment`. `--no-interpolate` stops both — variables stay as written and `env_file` is reported as a path only — so DevDock always passes it. The flag is a safety control, not a performance one, and the gated suite asserts the premise directly: one test confirms Compose still renders those secrets *without* the flag, so if that ever stops being true the test says so rather than the flag quietly becoming pointless.

Four facts survive parsing: service names, `depends_on` edges with their condition and Compose's `required` flag, and which services declare or disable a health check. Everything else is discarded, the document is not retained, and no part of it reaches an error. Errors name the failure and its exit status only. Service names are re-validated against Compose's own shape and, when one fails, the count is reported without the name, because an unexpected name is the value least safe to echo. A dependency condition outside the set Compose accepts today is reported as unsupported rather than downgraded to `service_started`: a newer Compose must not silently cause a service to start too early.

`list_declared_services`, which uses `config --services`, is deliberately kept for `status` and `down`. Those commands need names only, and `--services` discloses only names. The principle is to ask the runtime for the least it can give that answers the question.

A health check absent from the Compose file is not proof that a service has none, because the image may define `HEALTHCHECK`, which the configuration cannot show. Only `healthcheck: disable: true` is proof, so only that refuses a `service_healthy` wait before starting. The rest is reported by the wait, once a running container shows whether health is being reported at all.

`graph.py` turns those declarations plus the Compose file's own into a startup order. It takes plain mappings and returns either a `DependencyGraph` with sorted layers or a `GraphProblems` listing everything wrong at once, in the same `X | Y` return style as `plan_stop`. It holds no CLI codes: problems carry a structural `kind` and the command that builds the graph maps those to the codes a user sees, so the graph rules stay testable without the command vocabulary. Tarjan's algorithm is written iteratively because a deeply chained Compose file must produce a refusal rather than a `RecursionError`. No command builds a graph yet.

## Adapter boundary

All Docker invocation lives behind `RuntimeAdapter`. The Compose implementation builds argument arrays, never shell strings, runs each command with an explicit timeout, and returns status-only values (`ComposeCapabilities`, `ConfigValidation`, `ResourceInventory`, `ServiceObservations`) for everything inspect-side. The one exception is `OperationOutcome` from `start` and `stop`, which carries raw output in fields named `raw_` because a failed operation is not actionable without it; `lifecycle.py` is its only consumer and always passes it through `redaction` first. Diagnostics and later command handlers consume those values; tests inject a fake `CommandRunner` instead of patching module globals. `ownership.py` holds the pure rules for what DevDock owns: the derived project name and the label values Compose writes when DevDock runs it with an absolute `--project-directory` and `-f`. The adapter's inventory returns only identity fields and those labels, each JSON-encoded in the template so untrusted values cannot break the line format. Every Compose invocation receives a pinned environment (`HARDENED_ENVIRONMENT`) so project `.env` settings cannot change what a command removes. Lifecycle commands print the assessment before acting and refuse when any resource under the name is not provably theirs. Destructive cleanup and volume removal require a separate preview/authorization design.

SQLite is deferred until the product needs durable snapshots or recovery state. The first milestone has no daemon, web UI, cloud connection, credential store, or AI features.

