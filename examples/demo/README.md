# DevDock demo project

A disposable project for exercising DevDock against a real Docker daemon. It
creates only local throwaway resources: three containers, one network, and
one named volume, all under a Compose project name derived from this directory.

Run the scripted walkthrough from the repository root:

```bash
python scripts/demo.py
```

It copies this directory to a temporary location (so the Compose project name
is unique to the run), then steps through inspect, check, status, a waiting
`up`, a standalone `wait`, a repeated up, a `--dry-run` preview, an ownership
refusal, down, and a repeated down. It removes everything it created,
including the volume that DevDock deliberately preserves.

The project is built to show every readiness class in one run:

| Service | What it is | Reported as |
| --- | --- | --- |
| `migrate` | Runs once and exits `0` | `exited cleanly` |
| `web` | Health check *and* a published port with an HTTP probe | `ready`, with both sources listed |
| `worker` | Neither | `running (readiness unverified)` |

The `depends_on` entries give it a real startup order: nothing starts before
the migration finishes, and `worker` waits for `web` to be healthy. `devdock
up` starts one layer at a time and reports that order, and what proved each
service ready, with its result.

Add `--keep` to leave the project running for manual exploration; the script
prints the cleanup command.

## Doing it by hand

```bash
cp -r examples/demo /tmp/devdock-demo && cd /tmp/devdock-demo

devdock inspect .      # shows the derived Compose project name
devdock check .        # diagnostics; should pass with Docker running
devdock status .       # every service "missing" before the first start
devdock up .           # starts a layer at a time, then waits for readiness
devdock status .       # web "ready", worker "running (readiness unverified)"
devdock wait .         # starts nothing; already ready, so it returns at once
devdock up .           # idempotent; reconciles to the same state

devdock up . --no-wait        # start without waiting, as earlier versions did
devdock up . --require-ready  # refuse to accept a service nothing verifies
devdock down . --dry-run   # preview; changes nothing
devdock down . --yes       # stops containers and networks, preserves the volume
devdock down . --yes       # DOWN_NOTHING_TO_STOP

docker volume ls --filter "label=com.docker.compose.project=$(devdock inspect . \
  | sed -n 's/^Compose project name: //p')"   # the volume DevDock preserved
```

The services bind host port 18080. Change it in `compose.yaml` and the `ports`
entry in `devdock.yaml` if that port is taken.
