# CLI contract, version 0.1

This page describes behavior currently implemented by DevDock. Options may be added in later versions; commands that are not listed here are not implemented yet.

## Commands

| Command | Default directory | Behavior |
| --- | --- | --- |
| `devdock init [DIRECTORY]` | `.` | Creates the directory if needed, then writes a starter `devdock.yaml`. Refuses to overwrite an existing profile. |
| `devdock inspect [DIRECTORY]` | `.` | Finds the nearest project root and reports the profile and common Compose files. If a profile exists, validates it and reports the configured project, runtimes, and the Compose project name DevDock will manage. |
| `devdock up [DIRECTORY]` | `.` | Runs the profile's checks, proves ownership, validates the dependency graph, starts the project's Compose services detached, then waits for them to become ready. Refuses before any mutation if a check fails, the Compose file is missing or escapes the project, Docker is unavailable, the dependencies cannot produce a startup order, or any resource under the project name is not DevDock's. `--no-wait` starts without waiting. |
| `devdock wait [DIRECTORY]` | `.` | Waits for an already-started project to become ready. Starts, stops and changes nothing. |
| `devdock down [DIRECTORY]` | `.` | Previews then stops and removes the project's DevDock-managed containers and networks. Volumes are never removed. Requires confirmation or `--yes` when there is something to stop; `--dry-run` previews only. |
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
| `services` | Optional mapping of Compose service name to that service's `depends_on` and `readiness` declarations. Validated on load; see [Service dependencies and readiness probes](#service-dependencies-and-readiness-probes). |

Unknown fields are errors. The schema does not load `.env`, expand variables, execute commands, or store state. Python requirements currently accept a minimum such as `>=3.12` or `>=3.12.1`; the check compares major, minor, and patch versions with the interpreter running DevDock. Docker accepts `compose` and separately checks the CLI/plugin, daemon reachability, and Compose config parser. Reachability is probed with `docker version --format '{{.Server.Version}}'`, which fails when the server does not answer; `docker info` is not used because it exits successfully even when the daemon socket is unreachable. Other runtime names or requirement forms produce a failed finding.

### Service dependencies and readiness probes

The `services` block describes what DevDock should know about a Compose service beyond what the Compose file says. Both of its fields are optional and additive.

```yaml
services:
  web:
    depends_on:
      db: service_healthy
      migrate: service_completed_successfully
    readiness:
      - type: http
        port: 8080
        path: /healthz
  db:
    readiness:
      - type: tcp
        port: 5432
```

**This version validates these declarations and does nothing else with them.** Startup ordering and readiness waiting arrive with `devdock up --wait`; see [`ROADMAP.md`](../ROADMAP.md), milestone 3. Declaring them now does not change what `up`, `status`, or `down` do.

Service names, both as keys and as dependency targets, must match `[a-zA-Z0-9._-]+` and be at most 63 characters. That is Compose's own shape; a narrower rule would reject projects Compose runs. The rule keeps unprintable and structure-breaking names out, not names Compose considers ordinary. A service name that exists here but not in the Compose file is not an error at load time, because validating it requires running Compose; that check belongs to the command that uses the graph.

`depends_on` accepts either Compose's mapping form or its short list form, where `[db, cache]` means `service_started` for each. A service may not depend on itself. Longer cycles are not detected here, because they can run through the Compose file's own dependencies and so need the merged graph.

| Condition | Satisfied when |
| --- | --- |
| `service_started` | The dependency is running. |
| `service_healthy` | The dependency has a Compose health check and it reports healthy. |
| `service_completed_successfully` | The dependency exited with code `0`. |
| `service_ready` | The dependency satisfies *every* readiness source DevDock knows for it, including the probes below. A dependency with no readiness source at all counts as ready once it is running, since there is nothing further to check and the alternative is a wait that can never end. This condition is DevDock's own; Compose has no equivalent. |

#### How the two sources of dependencies combine

The Compose file and the profile both declare dependencies, and DevDock honors both rather than letting one replace the other. An edge declared in both places must satisfy both conditions.

`service_started`, `service_healthy`, and `service_ready` form one scale: each implies the one before it, so the stricter condition wins. One exception is carried separately rather than merged away: for a dependency that has *no* health check, `service_ready` is weaker than `service_healthy`, because readiness falls back to "running with nothing to check" while a health wait could never be met. So an edge that asked for health anywhere keeps that requirement on top of the merged condition, and adding a declaration can never make an edge laxer than one of its own halves. `service_completed_successfully` absorbs `service_started`, because a service that exited successfully did start. It **contradicts** `service_healthy` and `service_ready`, which require the service to still be running; DevDock reports that contradiction rather than picking one.

The merged graph must produce a startup order. These are reported together in one pass, so a project with several mistakes is not fixed one error at a time:

| Rejected | Reported as |
| --- | --- |
| A dependency naming a service the Compose file does not declare | The dependency and the list of declared services |
| A `services` key naming a service the Compose file does not declare | The name, with the suggestion to correct it or add the service |
| Two conditions on one edge that cannot both hold | Both conditions and where each came from |
| `service_healthy` on a service whose Compose file sets `healthcheck: disable: true` | That the condition can never be met, with three ways to fix it |
| A dependency cycle | One concrete path through each cyclic group, as `a depends on b depends on a`, plus every service involved |

A cycle is reported once per cyclic group with a single representative path, not once per distinct cycle, because enumerating every cycle can be exponential. Breaking the reported path and running again reveals whatever remains. Rejecting a condition does not hide a cycle the same edge takes part in.

A cycle written entirely inside the Compose file is rejected by Compose itself when DevDock lists the services, and surfaces as `UP_CONFIG_UNREADABLE` with Compose's own wording. `_DEPENDENCY_CYCLE` is for the cycles only the merged graph can see: ones where neither the Compose file nor `devdock.yaml` is cyclic on its own.

A `service_healthy` wait is only refused when the Compose file **disables** the health check. A service that simply does not declare one may still inherit a `HEALTHCHECK` from its image, which the Compose configuration cannot show, so refusing there would reject projects that work. That case is reported by the wait instead, once a running container shows whether health is being reported at all.

Compose's `required: false` on a dependency is honored: the edge still orders the start, but a dependency that never becomes available does not fail the service waiting on it. Declaring the same edge in `devdock.yaml` makes it required again, because writing it there is an explicit request.

DevDock reads dependencies with `docker compose config --format json --no-interpolate`. The flag matters: without it, Compose expands `${VAR}` from the shell and the project's `.env` and resolves every `env_file` into the rendered output, so the document would carry the project's secrets. With it, variables stay as written and `env_file` appears as a path. From that document DevDock keeps only service names, `depends_on` edges, and which services declare or disable a health check; the rest is discarded and no part of it appears in any message. A listing that fails reports the exit status, never the runtime's text. A dependency condition newer than the ones listed above is reported as unsupported rather than treated as `service_started`.

The startup order itself is a sequence of layers. Every service in a layer may start once every earlier layer has met its conditions, services within a layer are independent of one another, and both the layers and the services inside them are sorted, so the plan a user sees does not move between runs.

`readiness` lists the probes that must pass before DevDock calls a service ready. Every declared probe must pass, and a Compose health check, when the container has one, is checked as well. A service with no probes and no health check can only be reported as running; DevDock does not infer readiness from uptime.

| Probe field | Applies to | Contract |
| --- | --- | --- |
| `type` | both | Required, `tcp` or `http`. No other probe type exists; DevDock does not run commands supplied by a profile. |
| `port` | both | Required strict integer from `1` through `65535`. This is the port **published on the host**, not the port inside the container, because DevDock probes from the host. |
| `host` | both | Optional, default `127.0.0.1`. Must be loopback: a loopback IP address or `localhost`. Any other value is rejected when the profile loads, so a profile arriving with a cloned repository cannot direct DevDock at another machine. `localhost` is resolved by the host's own configuration. |
| `timeout` | both | Optional seconds for one probe attempt, greater than `0` and at most `60`. Default `2`. |
| `path` | `http` | Optional request target, default `/`. Must begin with `/`, be at most 2000 characters, be ASCII, and contain no whitespace, control characters, or `#`. Whitespace and control characters would let a declared path append a line to the request; a non-ASCII path cannot be sent at all; a fragment is stripped before sending, so the probe would contact something other than the target it reports. Percent-encode anything else. |
| `method` | `http` | Optional, `GET` or `HEAD`. Default `GET`. |
| `expect_status` | `http` | Optional list of accepted status codes from `100` through `599`. Omit it to accept `200` through `399`. An empty list is rejected as ambiguous. |

A service may declare at most eight readiness probes. Every one is attempted, in order, on each status report and on each poll of a wait.

HTTP probes use plain `http://`. A TLS endpoint should use a `tcp` probe for now; DevDock will not add an option to skip certificate verification.

#### What a probe does, and what it reports

A probe makes one bounded attempt. Before connecting, the declared host is checked against the loopback rule again and then resolved, and **every** address it resolves to must be loopback or the probe fails without connecting. DevDock then contacts that checked address rather than resolving a second time, so what was validated is what is reached. For an HTTP probe the declared host is still sent as the `Host` header, so name-based routing works.

HTTP probes send no credentials and no cookies, ignore any proxy configured in the environment, do not follow redirects, and **never read the response body**. A redirect is reported as its status code; following one would let the endpoint choose where DevDock connects next, which is what the loopback rule exists to prevent.

A probe reports one of these phrases and nothing else. No part of what the endpoint sent — no body, no header, no error text — is ever repeated, because a probe target can be made to answer with anything.

| Result | Meaning |
| --- | --- |
| `accepted a connection` | TCP only: the connection was established. The service is listening; whether it can serve is not known. |
| `answered N` | HTTP: status `N`, which the probe accepts. |
| `answered N, which is not accepted` | HTTP: status `N`, outside `expect_status` or the default `200`–`399`. |
| `refused the connection` | Nothing is listening on that port. |
| `accepted the connection, then closed it without answering` | HTTP: the port is open but the service hung up. This is normal for the whole window between a service binding its port and being able to serve, which is exactly what a probe is for. |
| `did not answer a readable HTTP response` | HTTP: something answered, but not HTTP. |
| `did not answer within Ns` | The attempt exceeded the probe's `timeout`. |
| `could not be reached` | The connection failed for another reason. |
| `could not be resolved` | The host could not be looked up. |
| `resolved to an address that is not loopback` | The name is loopback-shaped but this machine resolves it elsewhere. Nothing was contacted. |

A TCP probe can pass where an HTTP probe on the same port fails. That is the point of having both: an open port proves a process bound it, not that it is serving.

## Output and exit behavior

- `init` prints the created profile path to standard output and exits `0`. If the profile already exists, it writes an error to standard error and exits `1` without replacing the file.
- `inspect` prints the selected root and discovered files to standard output. Invalid profile data is reported on standard error and exits `1`; otherwise it exits `0`.
- An interrupt (Ctrl+C, or Ctrl+Break on Windows) during `up` or `down` stops the operation and exits `130` on every platform. The first interrupt asks the work to stop so DevDock can finish describing what it did; a second exits at once with the same status. Nothing is torn down either way — DevDock does not remove resources you did not ask it to remove, and `UP_INTERRUPTED` says what to run next.
- `up` reports a port probe that failed as `[ADVISORY]` rather than `[FAIL]`, since a published port is usually held by this project's own services and Compose's bind is authoritative. `up` prints its result as `[OK]` or `[REFUSED]` with a stable code, the ownership preview, and any redacted runtime output. Successful runs go to standard output and exit `0`; refusals and failures go to standard error and exit `1`.
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

DevDock then runs `docker compose --project-directory <root> --file <compose file> --project-name <derived name> up --detach`. It supplies those options itself and forwards no user-provided Compose options in this version. It passes `--remove-orphans=false` explicitly, so a project `.env` cannot turn orphan removal on, and never passes `--wait` or `--force-recreate`.

`up` returns when Compose has created and started the containers. That is not the same as ready: a container can be running while the service inside it is still starting. DevDock says so on success and does not poll for health. Readiness and dependency ordering are Milestone 3.

`--timeout SECONDS` bounds the run and defaults to 300. On timeout DevDock reports that containers may already have been created and points at the `docker ps` command for the project, because a timed-out start is not the same as a start that did nothing.

Repeating `up` with resources already running is safe; Compose reconciles to the same state and the ownership gate still passes because the resources are DevDock's.

### Failure classification

When a start or stop fails, DevDock reads the runtime's message to recognize *which kind* of failure it was, then discards the text. The code, summary, and suggested action are written in DevDock's own words and never quote the output, so classification cannot become a disclosure channel.

| Kind | Codes | Typical cause |
| --- | --- | --- |
| Permission | `UP_PERMISSION_DENIED`, `STATUS_PERMISSION_DENIED`, `DOWN_PERMISSION_DENIED` | The user cannot open the Docker socket. Reported before any action as well as from runtime output. |
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

On success, captured Docker output is not shown: DevDock has already described what changed in its own words, and the runtime's progress text adds only disclosure risk. When a failure is *recognized*, its output is withheld for the same reason: the code and suggested action already say what to do, and showing the text would risk disclosing values DevDock cannot recognize for no diagnostic gain. When a failure is *unrecognized*, DevDock prints the last 20 meaningful lines after redaction, because that is the only information available. `--show-output` prints all of both streams in either case, also redacted. `--show-output` controls volume, never redaction.

Any value from DevDock's own environment that is at least four characters long is replaced by `${NAME}`, except a short list of well-known non-secret variables such as `PATH`, `TERM`, `LANG`, and `LC_*`; `HOME` and `USER` are treated as private and redacted, so paths under your home directory appear as `${HOME}/...` in shown output; that is deliberate, since the paths identify the machine and user. Passwords inside `scheme://user:password@host` URLs are replaced with `[redacted]`. Values are matched in one pass, longest first, so a short value cannot rewrite another value's placeholder.

A second layer matches credentials by shape rather than by value, so it catches secrets DevDock has never seen — including values Compose read from the project's `.env`. It covers private key blocks, JWTs, AWS key IDs, GitHub and Slack tokens, `sk-` style API keys, `Bearer` headers, and `key=value` pairs whose key names a secret such as `POSTGRES_PASSWORD` or `api_key`. The key is kept and the value replaced, so the message still explains what went wrong.

Neither layer is a guarantee. A secret with no recognizable shape, not present in DevDock's environment, and not in a named assignment can still appear in output. Redaction also scans at most 256 KB; beyond that the output is truncated first.

This is a mitigation, not a guarantee. Compose also reads the project's `.env` file, which DevDock never opens, so a value that exists only there cannot be recognized and could appear in output. Treat `--show-output` as a deliberate disclosure and review what you paste elsewhere.

## Waiting for readiness

`devdock up` waits by default; `--no-wait` starts and returns. `devdock wait` does the waiting without starting anything, which is what to run after an interrupt, from a second terminal, or in a script that started the project some other way.

Before anything is started, the dependency graph is built and validated. Every problem it can find — a cycle, a dependency that does not exist, a condition that can never be met — would otherwise be discovered as a wait that never ends, so it is reported as a refusal instead, and nothing is mutated.

### What the wait does

Each poll takes one snapshot of the project: the ownership inventory, `docker compose ps`, and every readiness probe for every running service. A service is settled when it is not blocked and every readiness source agrees. The poll interval starts at 0.25 s and backs off to 2 s, so a long wait is not thousands of queries.

| Knob | Default | Meaning |
| --- | --- | --- |
| `--ready-timeout` | 60 s | How long one service may take. **Counted from when its dependencies are satisfied**, not from when the command started, so a service deep in the graph is not charged for its ancestors' slowness. |
| `--deadline` | 300 s | How long the whole startup may take. Overrides any remaining per-service budget. |
| `--require-ready` | off | Treat a service with no readiness source as *not* ready. By default such a service settles as running, because there is nothing further to check. |
| `--timeout` | 300 s | Unrelated to the above: how long the `docker compose up` command itself may run. |

### What ends a wait

A wait does not simply run out of time when it can say something better. These are checked in order of how much each explains:

| Outcome | Meaning |
| --- | --- |
| `_DEPENDENCY_REGRESSED` | A service that had become ready then exited, went unhealthy, or vanished. Its dependents are not left to time out one at a time. |
| `_CONDITION_UNSATISFIABLE` | A declared condition can never be met — waiting for health on a service that reports none, or for completion of one that exited non-zero. |
| `_SERVICE_DIED` | A service exited before it ever became ready. |
| `_SERVICE_UNSTABLE` | A service became ready and lost it three times. Reported rather than waited out; below that threshold, flapping is waited through, because a service that steadies is ready. |
| `_READINESS_TIMEOUT` | A service exceeded `--ready-timeout`. The last failing probe result is included. |
| `_DEADLINE_EXCEEDED` | The whole startup exceeded `--deadline`. |
| `_RUNTIME_UNOBSERVABLE` | Three consecutive status queries failed. |
| `_INTERRUPTED` | You pressed Ctrl+C. |

**Nothing is torn down on any of these.** DevDock does not remove resources you did not ask it to remove; each result says what to run next.

## Stopping services

`devdock down` is the only destructive command. It always prints a plan first:

- containers it will stop and remove, by name, service, and state;
- project networks it will remove;
- DevDock containers it will leave in place because their service is no longer declared in the current Compose file;
- volumes it will preserve. No DevDock command removes volumes in this version; that is later roadmap work with its own authorization design.

The plan is built from the same gates as `up` except the profile's diagnostics, which are deliberately skipped so a project can be torn down even when, say, a required environment variable is unset. Any ownership conflict, incomplete inventory, or unreadable Compose file refuses before the plan is shown.

Then one of three things happens. `--dry-run` prints `DOWN_DRY_RUN` and exits `0` without changing anything. Otherwise, if there is nothing to stop, DevDock reports `DOWN_NOTHING_TO_STOP` and runs no command; repeating `down` is safe. If there is work to do, DevDock asks for confirmation on an interactive terminal, or requires `--yes` when standard input is not a terminal, and refuses with `DOWN_CONFIRMATION_REQUIRED` otherwise. Declining prints `DOWN_CANCELLED` and exits `1` with nothing changed.

Immediately before acting, DevDock re-reads the inventory. If the set of managed containers changed while the prompt was open, or a conflict appeared, it refuses with `DOWN_PLAN_STALE` and nothing is run; rerun `down` to see the current plan. The comparison covers containers; a network or volume that appears in that window is not detected.

When a DevDock container whose service is no longer declared is left in place and is running, the project network is left too, because that container still uses it; the plan says so. If the leftover is stopped, Docker has already released its network endpoint and the network is removed as usual.

The command run is `docker compose --project-directory <root> --file <compose file> --project-name <derived name> down --remove-orphans=false`, with no `--volumes` or `--rmi`. After it returns DevDock re-inventories the project: `DOWN_STOPPED` means the re-inventory succeeded and no planned container or network survived; `DOWN_PARTIAL` is returned, with exit `1`, when something remains or when the re-inventory itself failed, so DevDock never claims success it could not verify. A timeout reports that some resources may already be gone and points at `devdock status`. Runtime output is handled exactly as for `up`.

## Reporting status

`devdock status` is read-only. It needs a Compose file inside the project and a responsive Docker daemon; it does not require the profile's checks to pass, because a status report is most useful when something is wrong.

It combines three sources: the ownership inventory (which containers are provably DevDock's), `docker compose config --services` (which services the current Compose file declares; names only, no rendered values), and `docker compose ps --all --format json` (state, health, exit code, published ports). From the runtime report DevDock keeps only ID, name, service, state, health, exit code, and publishers; image, command, and labels are discarded at parse time. It then runs the readiness probes the profile declares for each **running** service.

### Readiness sources

A service's readiness sources are its Compose health, when the container reports any, plus every `tcp` or `http` probe declared for it under `services` in `devdock.yaml`. A service is `ready` only when **every** one of its sources agrees. A service with no source at all can only be shown to be running.

Each source is listed under the table with its own result, whether it passed or not, because a readiness class alone does not say what was checked:

```text
SERVICE  CONTAINER          STATE    HEALTH   READINESS       PORTS
web      ...-web-1          running  healthy  ready           127.0.0.1:19311->80/tcp
api      ...-api-1          running  -        starting        -
migrate  ...-migrate-1      exited   -        exited cleanly  -
  web: [PASS] compose health reports healthy
  web: [PASS] http GET 127.0.0.1:19311/healthz answered 200
  api: [FAIL] tcp 127.0.0.1:19312 refused the connection
  migrate: exited with code 0
Summary: 1 exited cleanly, 1 ready, 1 starting.
```

Only running services are probed. A stopped service's port is free, so whatever answers there belongs to something else. If a probe fails on a port the service does not publish, DevDock says so in a note: probing the port inside the container rather than the published one is the usual cause.

| Readiness | Meaning |
| --- | --- |
| `ready` | Running, and every readiness source agrees. |
| `running (readiness unverified)` | Running with no readiness source at all. DevDock can show the container is alive, not that the service is serving. |
| `starting` | Running; at least one source has not passed yet. |
| `unhealthy` | Running; the Compose health check is failing. |
| `exited cleanly` | Exited with code `0`. This is what a completed one-shot task looks like; it is also what a service that was stopped gracefully looks like, which Docker does not distinguish either. |
| `stopped` | The container exists, is not running, and did not exit with `0`. The exit code is shown when known. |
| `missing` | Declared in the Compose file; no DevDock-managed container exists. |
| `unknown` | The runtime reported a state DevDock does not model. |

A failing probe reports `starting`, not `unhealthy`. One sample is not evidence that a service is broken — a probe failing is exactly what a service looks like while it is coming up, and unlike a Compose health check a probe has no retry policy of its own to have exhausted. Compose health is the exception: Docker already applied one before calling a container unhealthy, so a reported `unhealthy` is taken at its word and outranks passing probes.

Services are listed in declared order, one row per container when a service has replicas, followed by any DevDock-managed containers no longer declared in the current Compose file. Runtime-reported containers DevDock cannot prove it created are ignored and counted in a note. Resources under the project name that fail the ownership rules are listed separately as not managed.

Exit code is `0` when the report is complete and `1` when a required input is missing or any runtime query failed; a partial table is still printed in that case. Readiness does not affect the exit code yet; scripts should wait for the planned machine-readable output rather than parse this table.

### How commands are run

Every Docker command is an argument array with an explicit timeout, started in a process group of its own. The isolation matters for interrupts: a terminal delivers Ctrl+C to every process in the foreground group, so without it the terminal would be interrupting `docker` directly, part-way through whatever it was doing, and DevDock would only learn about it afterwards. With it, the interrupt arrives at DevDock, which decides what the child does and reports what happened.

On POSIX the whole group is signalled when a command is cancelled or times out, so a helper process the command started is not left behind; it is asked to stop first and killed if it does not. Windows has no graceful equivalent — termination there is immediate and reaches the command itself, not anything it started. A command leaves no running child behind on any exit path, including an interrupt.

Captured output is written to a temporary file rather than a pipe, so a command that produces a great deal of output cannot fill a buffer and stall, and partial output survives a timeout or an interrupt. DevDock reads back at most 1 MB and says so when it stops. Line endings are normalized, so a Windows command's output is the same string a POSIX one's would be and every rule downstream behaves identically.

### Environment handed to Compose

DevDock runs Compose with its own environment plus three pinned settings: `COMPOSE_REMOVE_ORPHANS=false`, `COMPOSE_ANSI=never`, and `COMPOSE_PROGRESS=plain`. Compose reads `COMPOSE_*` settings from the project's `.env`, which DevDock treats as untrusted; shell values take precedence over `.env` in Compose, so these pins keep the `down` preview truthful and the captured output free of colour codes whatever `.env` says. Other `.env` content, including `DOCKER_HOST` or `DOCKER_CONTEXT`, is honoured by Compose exactly as it would be if you ran `docker compose` in that directory yourself; DevDock does not read the file to check.

## Trying the commands

`examples/demo` is a disposable project with two services, one with a health check and one without, plus a named volume. `python scripts/demo.py` runs the full lifecycle against a real daemon and cleans up afterwards.

## Ownership scope

`up` and `down` act only on Compose resources DevDock can prove it created, and `status` reports which resources those are. `inspect` prints the Compose project name DevDock derives for a profile so the resources are recognizable in `docker ps`.

- The project name is `devdock-<slug>-<digest>`. `<slug>` is the profile's `project` lowercased with every run of non-alphanumeric characters replaced by one hyphen, trimmed, and limited to 40 characters (`project` if nothing remains). `<digest>` is the first 12 hex characters of the SHA-256 of the resolved project root path, case-folded on Windows. The same project at the same directory always maps to the same name; a copy elsewhere gets a different one; symlinked paths to one directory share one name.
- Before acting, DevDock lists containers under that name (by Compose project label and by anchored name prefix), plus labeled networks and volumes. A container counts as managed only if Compose's own labels show it was created from this project root, from exactly this Compose file, as a service rather than a one-off `compose run`.
- Any container under the name that fails those checks, any unparseable runtime output, or any failed inventory query makes DevDock refuse the action and print why, with a `docker ps` command the user can run to inspect the resources. DevDock never adopts resources on the strength of a familiar name.
- Changing `compose_file` or moving the project while resources exist is reported as a mismatch rather than silently creating a parallel project; stop the old resources with the configuration that created them first.
- Volumes under the name are listed as preserved. No DevDock command removes volumes in this version.

Only identity fields and the ownership labels above are requested from Docker, each JSON-encoded by the template so a tab or newline inside a label cannot forge an extra record; container environments, images, and full label sets are never read for this purpose.

### Limits of label-based ownership

This model protects against accidental overlap: another project, a renamed directory, a stale configuration, or a one-off container. It is not a defence against a hostile local process with access to the Docker socket. Such a process can set exactly the labels DevDock looks for and make its container count as managed, and it can already control everything Docker manages. Networks and volumes are matched by the project label alone because Compose records no working-directory or file labels on them. Durable records of what DevDock itself created are later roadmap work and will tighten these rules.

## Diagnostic codes

Each `check` result and each lifecycle result carries a stable code. This table is complete: a test fails if the source emits a code that is not listed here. Existing codes are never repurposed for a different meaning. The human-readable output around them is not a scripting API; machine-readable output is planned.

### `check` findings

| Code | Severity | Meaning |
| --- | --- | --- |
| `PROFILE_VALID` | PASS | The profile parsed and validated. |
| `PROJECT_PYTHON_METADATA` | PASS or WARN | Python version metadata was found. PASS when DevDock runs from the project's own virtual environment; WARN when the target interpreter was not inspected. |
| `PROJECT_PYTHON_METADATA_INVALID` | WARN | A recognized Python metadata file could not be read. |
| `PYTHON_VERSION_OK` | PASS | The interpreter running DevDock meets the declared minimum. |
| `PYTHON_VERSION_TOO_OLD` | FAIL | It does not. |
| `PYTHON_REQUIREMENT_UNSUPPORTED` | FAIL | The Python requirement is not a `>=X.Y` or `>=X.Y.Z` minimum. |
| `RUNTIME_UNSUPPORTED` | FAIL | A declared runtime other than `python` or `docker`. |
| `DOCKER_REQUIREMENT_UNSUPPORTED` | FAIL | The Docker requirement is not `compose`. |
| `DOCKER_CHECKS_SKIPPED` | SKIP | Docker checks did not run because the requirement is unsupported. |
| `COMPOSE_FILE_FOUND` | PASS | The configured or discovered Compose file exists inside the project. |
| `COMPOSE_FILE_MISSING` | FAIL | No Compose file was configured or discovered, or the configured one does not exist. |
| `COMPOSE_FILE_OUTSIDE_PROJECT` | FAIL | The Compose file resolves outside the project after following symlinks. |
| `DOCKER_CLI_MISSING` | FAIL | No `docker` executable on `PATH`. |
| `COMPOSE_CLI_AVAILABLE` | PASS | `docker compose version` succeeded. |
| `COMPOSE_CLI_UNAVAILABLE` | FAIL | The Compose plugin did not respond or exited non-zero. |
| `DOCKER_DAEMON_AVAILABLE` | PASS | `docker version` returned a server version. |
| `DOCKER_DAEMON_UNAVAILABLE` | FAIL | The daemon did not answer. |
| `DOCKER_DAEMON_PERMISSION_DENIED` | FAIL | The daemon socket exists but this user may not open it. |
| `DOCKER_DAEMON_SKIPPED` | SKIP | The daemon check did not run because the CLI or plugin is missing. |
| `COMPOSE_CONFIG_VALID` | PASS | `docker compose config` accepted the file. |
| `COMPOSE_CONFIG_INVALID` | FAIL | It rejected the file. Output is suppressed. |
| `COMPOSE_CONFIG_UNAVAILABLE` | FAIL | The parser could not be run or timed out. |
| `COMPOSE_CONFIG_SKIPPED` | SKIP | The parser was not run because a prerequisite failed or there is no safe file. |
| `ENVIRONMENT_VARIABLE_SET` | PASS | The required variable is set and non-empty. Value redacted. |
| `ENVIRONMENT_VARIABLE_UNSET` | FAIL | It is not set. |
| `ENVIRONMENT_VARIABLE_EMPTY` | FAIL | It is set to an empty value. |
| `REQUIRED_FILE_FOUND`, `REQUIRED_DIRECTORY_FOUND` | PASS | The required path exists with the right type. |
| `REQUIRED_FILE_MISSING`, `REQUIRED_DIRECTORY_MISSING` | FAIL | It does not exist or has the wrong type. |
| `REQUIRED_FILE_OUTSIDE_PROJECT`, `REQUIRED_DIRECTORY_OUTSIDE_PROJECT` | FAIL | It resolves outside the project. |
| `REQUIRED_FILE_UNRESOLVABLE`, `REQUIRED_DIRECTORY_UNRESOLVABLE` | FAIL | It could not be resolved, for example a symlink loop. |
| `PORT_AVAILABLE` | PASS | A loopback bind succeeded at the moment of the check. |
| `PORT_UNAVAILABLE` | FAIL in `check`; `ADVISORY` in `up` | The bind failed. During `up` this is reported but does not block the start. |

### Lifecycle gates

These stop a command before anything runs. The prefix names the command.

| Code suffix | Commands | Meaning |
| --- | --- | --- |
| `_PREFLIGHT_FAILED` | `UP` | A `check` finding other than an advisory one failed. The failures are listed. |
| `_COMPOSE_FILE_MISSING` | `UP`, `STATUS`, `DOWN` | No Compose file is configured, discoverable, or present. |
| `_COMPOSE_FILE_OUTSIDE_PROJECT` | `UP`, `STATUS`, `DOWN` | The Compose file resolves outside the project. |
| `_RUNTIME_UNAVAILABLE` | `UP`, `STATUS`, `DOWN` | The Compose CLI is unavailable, or the Docker executable could not be started. |
| `_DAEMON_UNAVAILABLE` | `UP`, `STATUS`, `DOWN` | The daemon did not answer, before or during the operation. |
| `_PERMISSION_DENIED` | `UP`, `STATUS`, `DOWN` | This user may not open the daemon socket, before or during the operation. |
| `_OWNERSHIP_CONFLICT` | `UP`, `DOWN` | A resource under the project name is not provably DevDock's, or the inventory was incomplete. |
| `_CONFIG_UNREADABLE` | `DOWN` | The Compose file's services could not be listed, so the plan cannot be built. |
| `_CONFIRMATION_REQUIRED` | `DOWN` | Standard input is not a terminal and `--yes` was not given. |
| `_CANCELLED` | `DOWN` | The user declined the prompt. |
| `_PLAN_STALE` | `DOWN` | The managed containers changed between the plan and the confirmation. |
| `_INTERRUPTED` | `UP`, `DOWN` | You interrupted the operation. Nothing was removed; what already existed is left as it was. Exit status `130`. |

### Dependency and readiness codes

| Code suffix | Commands | Meaning |
| --- | --- | --- |
| `_DEPENDENCY_CYCLE` | `UP`, `WAIT` | The declared dependencies contain a cycle. One path through each cyclic group is printed. |
| `_DEPENDENCY_CONFLICT` | `UP`, `WAIT` | Two declarations of one edge cannot both hold, such as `service_healthy` and `service_completed_successfully`. |
| `_SERVICE_UNDECLARED` | `UP`, `WAIT` | `devdock.yaml` configures a service the Compose file does not declare. |
| `_DEPENDENCY_UNDECLARED` | `UP`, `WAIT` | A dependency names a service that does not exist. |
| `_DEPENDENCY_UNHEALTHCHECKED` | `UP`, `WAIT` | A `service_healthy` wait on a service whose Compose file disables its health check. |
| `_DEPENDENCY_UNSUPPORTED` | `UP`, `WAIT` | Compose reported a dependency condition this version cannot order against. Start with `--no-wait` or upgrade DevDock. |
| `_READY` | `UP`, `WAIT` | Every service is ready, and each was verified by a source. Exit `0`. |
| `_READY_UNVERIFIED` | `UP`, `WAIT` | Every service settled, but some have no readiness source, so DevDock can show they are running and not that they are serving. Exit `0`. The services are named. |
| `_READINESS_TIMEOUT` | `UP`, `WAIT` | A service exceeded `--ready-timeout`. |
| `_DEADLINE_EXCEEDED` | `UP`, `WAIT` | The startup exceeded `--deadline`. |
| `_SERVICE_DIED` | `UP`, `WAIT` | A service failed before becoming ready. |
| `_SERVICE_UNSTABLE` | `UP`, `WAIT` | A service kept losing readiness. |
| `_DEPENDENCY_REGRESSED` | `UP`, `WAIT` | A service that had become ready did not stay that way. |
| `_CONDITION_UNSATISFIABLE` | `UP`, `WAIT` | A declared condition can never be met. |
| `_RUNTIME_UNOBSERVABLE` | `UP`, `WAIT` | The runtime stopped answering during the wait. |
| `GRAPH_UNAVAILABLE` | warning | The Compose file's dependencies could not be read, so startup order is not verified. The wait still runs. |

### Lifecycle outcomes

| Code | Exit | Meaning |
| --- | --- | --- |
| `UP_STARTED` | 0 | Compose started the services and `--no-wait` was given, so readiness was not verified. |
| `UP_TIMED_OUT`, `DOWN_TIMED_OUT` | 1 | The operation exceeded `--timeout`; resources may be partially created or removed. |
| `STATUS_REPORTED` | 0 | The report is complete. |
| `STATUS_INCOMPLETE` | 1 | A runtime query failed; the partial report is still shown. |
| `DOWN_DRY_RUN` | 0 | The plan was shown and nothing changed. |
| `DOWN_NOTHING_TO_STOP` | 0 | No removable container or network exists; no command was run. |
| `DOWN_STOPPED` | 0 | Everything the plan said would be removed is gone, and the re-inventory succeeded. |
| `DOWN_PARTIAL` | 1 | Something planned for removal survived, or the result could not be verified. |

### Recognized runtime failures

When Compose exits non-zero, DevDock classifies its message and reports one of these with the command prefix (`UP_` or `DOWN_`). Output is hidden for recognized kinds because the code and suggested action already explain the problem.

| Code suffix | Meaning |
| --- | --- |
| `_PERMISSION_DENIED` | Docker refused the connection for this user. |
| `_DAEMON_UNAVAILABLE` | The daemon stopped responding during the operation. |
| `_CONFIG_INVALID` | Compose rejected the configuration at run time. |
| `_REGISTRY_AUTH_REQUIRED` | The registry needs `docker login`. |
| `_IMAGE_NOT_FOUND` | The image or tag does not exist. |
| `_PULL_FAILED` | The image could not be downloaded: network, proxy, timeout, TLS, or rate limit. |
| `_PORT_CONFLICT` | A published port is already in use. |
| `_NETWORK_CONFLICT` | A network exists or an address pool overlaps. |
| `_RESOURCE_EXHAUSTED` | Out of disk space, memory, or file handles. |
| `_RESOURCE_IN_USE` | A network or volume is still attached. |
| `_SERVICE_FAILED` | A container exited or a dependency never became healthy. |
| `_FAILED` | Not recognized. A redacted tail of the output is shown. |
