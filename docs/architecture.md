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

Command execution (adapters/compose.py)
  ├─ argument arrays, never shell strings; explicit timeout on every call
  ├─ child in its own process group so a terminal's Ctrl+C reaches DevDock, not docker
  ├─ cancellation polled while waiting; POSIX signals the group so helpers go too
  └─ output captured to a temporary file, bounded on read, newlines normalized

Runtime adapter (adapters/)
  ├─ RuntimeAdapter protocol: capability detection, config validation, resource inventory
  ├─ ComposeAdapter: argument arrays, bounded timeouts, injectable CommandRunner
  └─ results carry status only; captured output is never copied into findings

Lifecycle (lifecycle.py)
  ├─ policy only: every gate runs before the adapter is asked to mutate
  ├─ down is split into plan_stop (preview) and execute_stop (re-check, act, verify)
  └─ returns redacted display lines and a stable code

Status (status.py)
  ├─ readiness classes: ready, running, starting, unhealthy, exited_ok, stopped, missing, unknown
  ├─ classify_readiness(state, health, exit_code, sources) is the single source of that mapping
  ├─ a service is ready only when every source agrees; no source means running, not ready
  ├─ decides what to probe (running services only); the injected probe callable connects
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

Wait engine (orchestrate.py)
  ├─ injected Clock, Observer, and CancelToken; nothing here sleeps or runs a command
  ├─ a service's own budget starts when it is running and unblocked, not when the wait began
  ├─ fails fast on a regression or an unmeetable condition rather than timing out
  └─ flapping is counted and reported; stability is decided by consecutive successes

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

`status.py` separates *running* from *ready*, and now evaluates every source a service has: its Compose health, when the container reports any, plus each `tcp` or `http` probe the profile declares. A service is `ready` only when all of them agree, and one with no source at all is `running` — alive, unverified. Each source's result is kept per service and printed, because a readiness class on its own does not say what was checked.

The module stays pure. It decides *what* is worth probing — only running services, since a stopped service's port is free and whatever answers on it belongs to something else — and the probe callable it is handed does the connecting, defaulting to the real one and replaced in tests. That is the same shape as `lifecycle` defaulting to a real `ComposeAdapter`.

Two classification rules are deliberate. A failing probe yields `starting` rather than `unhealthy`, because one sample is not evidence of a broken service and a probe, unlike a Compose health check, has no retry policy of its own to have exhausted; Compose health is taken at its word because Docker already applied one. And `exited_ok` separates an exit code of `0` from any other, which is what `service_completed_successfully` needs — while being honest that Docker cannot distinguish a completed task from a service stopped gracefully.

### Waiting, and why each rule is there

`orchestrate.py` polls snapshots until every service has settled. Its clock, its observer, and its cancellation flag are all injected, so the whole module is exercised without sleeping: a three-hundred-second deadline is reached in a few hundred iterations of arithmetic, which is what makes slow starts, flapping, and timeouts testable at all.

Four rules carry most of the behavior.

**A service's budget starts when it is running and unblocked**, not when the wait began. Without that, a service deep in the graph is charged for the time its ancestors took and times out for someone else's slowness. If a dependency later stops being satisfied, the clock is cleared again rather than continuing to run.

**A blocked service is never settled**, however healthy it looks. The dependency condition was declared for a reason, and reporting success while one goes unmet would hide exactly what the wait was asked to watch — a service running happily against a database that never came up.

**A regression fails fast.** A service that had succeeded and then exits, goes unhealthy, or becomes unrecognizable has gone somewhere it will not return from, and everything depending on it should not spend its own budget discovering that one service at a time. Merely losing readiness is *not* a regression: dropping back to `starting` is flapping, which is counted, reported, and waited through, because the consecutive-success requirement is what decides whether a service has steadied.

**A condition that can never be met is named rather than waited out.** Waiting for health on a service that reports none would otherwise consume the dependent's whole budget and then blame the timeout instead of the cause. This is where the check deferred from the Compose configuration reader lands: absence of a health check in the Compose file is not proof, but a *running* container still reporting no health after a short grace period is.

A condition is a statement about the dependency alone. `service_healthy` asks whether its health check passes, exactly as it does in Compose, and says nothing about whether that dependency's own dependencies are satisfied. So the engine does not guarantee that a dependency settles before its dependent; it guarantees that nothing is reported ready with a condition outstanding.

`ProjectStatus.all_ready` remains the single-sample predicate. The temporal policy — how many consecutive successes a source needs, flapping, deadlines — belongs to the wait engine, which has more than one sample to reason about.

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

## Interrupts

`cancellation.py` holds one thing, a `CancelToken`, and imports nothing. Both ends of the system need it — the command runner, to abandon a child, and the wait engine, to stop polling — and putting it anywhere else would make one of those depend on the other.

The runner starts every child in a process group of its own. That is the whole reason interrupts work: a terminal delivers Ctrl+C to every process in the foreground group, so without it the terminal interrupts `docker` directly, part-way through whatever it was doing, and DevDock learns about it afterwards. The two platforms spell the request differently, and the difference is not symmetric — Windows *accepts* `start_new_session` and ignores it rather than refusing it, so a wrong branch there would isolate nothing and say nothing. The choice is therefore a pure function returning a value, asserted for both platforms on every platform, rather than a conditional buried in the call.

Termination differs honestly. On POSIX the process group is signalled, asked first and killed if it does not go, so a helper the command started is not left behind; the group id is the child's pid, because it leads the group it was given, and reading it back would be a second chance to fail. Windows has no graceful equivalent and reaches only the command itself. Every exit path from the runner — returning, timing out, cancelling, or any exception including `SystemExit` raised by a signal handler — passes through a `finally` that leaves no running child, because a child outliving the command that started it is the one outcome with no way back.

Capture goes to a temporary file rather than a pipe. A command cannot stall on a full buffer, partial output survives a timeout or an interrupt, and how much is read back is DevDock's decision rather than the child's. Newlines are normalized on read so a Windows command's output is the same string a POSIX one's would be and every rule downstream — classification, redaction, line counting — behaves identically.

The CLI installs handlers only for the length of an operation and always restores them, so DevDock does not change what an interrupt means anywhere else. The first one asks the work to stop, so the command can finish describing what it did; the second raises `SystemExit(130)` directly, which Click passes through, where a `KeyboardInterrupt` would have become "Aborted!" and exit 1. Both paths exit `130` on every platform, and neither removes anything: tearing a project down is an action the user did not ask for, and milestone 4 owns recovery.

SQLite is deferred until the product needs durable snapshots or recovery state. The first milestone has no daemon, web UI, cloud connection, credential store, or AI features.

