# DevDock demo project

A disposable project for exercising DevDock against a real Docker daemon. It
creates only local throwaway resources: two containers, one network, and one
named volume, all under a Compose project name derived from this directory.

Run the scripted walkthrough from the repository root:

```bash
python scripts/demo.py
```

It copies this directory to a temporary location (so the Compose project name
is unique to the run), then steps through inspect, check, status, up, a
repeated up, a `--dry-run` preview, an ownership refusal, down, and a repeated
down. It removes everything it created, including the volume that DevDock
deliberately preserves.

Add `--keep` to leave the project running for manual exploration; the script
prints the cleanup command.

## Doing it by hand

```bash
cp -r examples/demo /tmp/devdock-demo && cd /tmp/devdock-demo

devdock inspect .      # shows the derived Compose project name
devdock check .        # diagnostics; should pass with Docker running
devdock status .       # every service "missing" before the first start
devdock up .           # starts both services detached
devdock status .       # web "ready", worker "running (readiness unverified)"
devdock up .           # idempotent; reconciles to the same state
devdock down . --dry-run   # preview; changes nothing
devdock down . --yes       # stops containers and networks, preserves the volume
devdock down . --yes       # DOWN_NOTHING_TO_STOP

docker volume ls --filter "label=com.docker.compose.project=$(devdock inspect . \
  | sed -n 's/^Compose project name: //p')"   # the volume DevDock preserved
```

The services bind host port 18080. Change it in `compose.yaml` and the `ports`
entry in `devdock.yaml` if that port is taken.
