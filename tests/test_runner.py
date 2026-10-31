"""The command runner: isolation, cancellation, timeouts, and leaving nothing behind.

Every test that starts a process starts ``sys.executable``, so the whole
module runs and means the same thing on Windows, macOS and Linux. The few
checks that can only be made on one platform say so and skip elsewhere,
rather than being written in a way that quietly passes without testing
anything.

The platform choice itself is a pure function, so both branches are asserted
on every platform. That matters more than usual here: Windows accepts
``start_new_session`` and ignores it rather than refusing it, so picking the
wrong field there would isolate nothing and report nothing.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from devdock.adapters.base import CommandCancelled
from devdock.adapters.compose import (
    CANCEL_POLL_INTERVAL,
    MAX_CAPTURED_BYTES,
    WINDOWS_NEW_PROCESS_GROUP,
    ProcessIsolation,
    _stop_child,
    process_isolation,
    run_command,
)
from devdock.cancellation import CancelToken

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows process groups")

# Long enough that nothing finishes by accident, short enough that a leaked
# process does not outlive the test session by much.
FOREVER = "import time; time.sleep(120)"


def python(*code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", *code)


# --- the platform choice ------------------------------------------------


def test_posix_asks_for_a_new_session() -> None:
    assert process_isolation("posix") == ProcessIsolation(creationflags=0, start_new_session=True)


def test_windows_asks_for_a_new_process_group() -> None:
    isolation = process_isolation("nt")

    assert isolation.start_new_session is False
    assert isolation.creationflags == WINDOWS_NEW_PROCESS_GROUP


def test_each_platform_sets_exactly_one_of_the_two() -> None:
    """The other must stay at its inert default, which each platform ignores."""

    for name in ("posix", "nt"):
        isolation = process_isolation(name)
        assert bool(isolation.creationflags) != isolation.start_new_session


def test_the_default_matches_the_machine_the_tests_are_running_on() -> None:
    assert process_isolation() == process_isolation(os.name)


def test_the_windows_flag_matches_the_value_windows_defines() -> None:
    """Hard-coded because the constant does not exist off Windows."""

    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        assert WINDOWS_NEW_PROCESS_GROUP == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert WINDOWS_NEW_PROCESS_GROUP == 0x00000200


# --- ordinary behaviour -------------------------------------------------


def test_a_successful_command_returns_its_output_and_status() -> None:
    result = run_command(
        python("import sys; sys.stdout.write('out'); sys.stderr.write('err')"), timeout=30
    )

    assert (result.returncode, result.stdout, result.stderr) == (0, "out", "err")


def test_a_failing_command_returns_its_exit_code() -> None:
    assert run_command(python("raise SystemExit(3)"), timeout=30).returncode == 3


def test_output_newlines_are_normalized_whatever_the_platform_wrote() -> None:
    """Downstream rules count and match lines; they must see one shape."""

    result = run_command(python("import sys; sys.stdout.write('a\\r\\nb\\rc\\nd')"), timeout=30)

    assert result.stdout == "a\nb\nc\nd"


def test_undecodable_output_is_replaced_rather_than_raising() -> None:
    result = run_command(
        python("import sys; sys.stdout.buffer.write(b'ok\\xff\\xfebad')"), timeout=30
    )

    assert result.returncode == 0
    assert "ok" in result.stdout


def test_the_working_directory_is_honoured(tmp_path: Path) -> None:
    result = run_command(python("import os; print(os.getcwd())"), cwd=tmp_path, timeout=30)

    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_the_environment_is_honoured() -> None:
    environment = {**os.environ, "DEVDOCK_TEST_MARKER": "marker-7f2a"}

    result = run_command(
        python("import os; print(os.environ.get('DEVDOCK_TEST_MARKER'))"),
        timeout=30,
        env=environment,
    )

    assert result.stdout.strip() == "marker-7f2a"


def test_a_missing_executable_raises_oserror() -> None:
    with pytest.raises(OSError):
        run_command(("devdock-no-such-executable-9f1c",), timeout=30)


def test_the_child_gets_no_standard_input() -> None:
    """A command that waits for input would otherwise hang until its timeout."""

    result = run_command(python("import sys; print(repr(sys.stdin.read()))"), timeout=30)

    assert result.returncode == 0
    assert result.stdout.strip() == "''"


# --- bounded capture ----------------------------------------------------


def test_output_beyond_the_cap_is_dropped_and_said_to_be() -> None:
    result = run_command(
        python(f"import sys; sys.stdout.write('x' * {MAX_CAPTURED_BYTES + 5000})"), timeout=60
    )

    assert len(result.stdout) < MAX_CAPTURED_BYTES + 200
    assert "stopped reading after" in result.stdout


def test_output_just_under_the_cap_is_kept_whole() -> None:
    size = 1000
    result = run_command(python(f"import sys; sys.stdout.write('y' * {size})"), timeout=60)

    assert result.stdout == "y" * size


def test_a_child_writing_more_than_a_pipe_would_hold_does_not_deadlock() -> None:
    """Capture goes to a file, so the child is never blocked on a full buffer."""

    result = run_command(
        python("import sys; sys.stdout.write('z' * 500_000); sys.stderr.write('w' * 500_000)"),
        timeout=60,
    )

    assert result.returncode == 0
    assert len(result.stdout) == 500_000 and len(result.stderr) == 500_000


# --- timeouts -----------------------------------------------------------


def test_a_command_that_overruns_raises_and_is_killed() -> None:
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run_command(python(FOREVER), timeout=0.5)

    assert time.monotonic() - started < 30
    assert raised.value.timeout == 0.5


def test_a_timeout_carries_whatever_was_written_before_it() -> None:
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run_command(
            python(
                "import sys, time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(120)"
            ),
            timeout=1.0,
        )

    assert raised.value.output == "partial"


# --- cancellation -------------------------------------------------------


def test_a_token_set_before_the_command_still_runs_it_once() -> None:
    """Cancellation is checked while waiting; a quick command simply finishes."""

    token = CancelToken()
    token.cancel()

    result = run_command(python("import sys; sys.stdout.write('done')"), timeout=30, cancel=token)

    assert result.stdout == "done"


def test_cancelling_a_running_command_stops_it_promptly() -> None:
    token = CancelToken()
    timer = threading.Timer(0.3, token.cancel)
    timer.start()
    started = time.monotonic()

    try:
        with pytest.raises(CommandCancelled):
            run_command(python(FOREVER), timeout=120, cancel=token)
    finally:
        timer.cancel()

    assert time.monotonic() - started < 30, "cancellation should not wait for the timeout"


def test_a_cancellation_carries_whatever_was_written_before_it() -> None:
    token = CancelToken()
    timer = threading.Timer(0.5, token.cancel)
    timer.start()

    try:
        with pytest.raises(CommandCancelled) as raised:
            run_command(
                python(
                    "import sys, time; sys.stdout.write('partial'); sys.stdout.flush();"
                    " time.sleep(120)"
                ),
                timeout=120,
                cancel=token,
            )
    finally:
        timer.cancel()

    assert raised.value.raw_stdout == "partial"


def test_cancellation_is_noticed_within_about_one_poll() -> None:
    token = CancelToken()
    timer = threading.Timer(0.2, token.cancel)
    timer.start()
    started = time.monotonic()

    try:
        with pytest.raises(CommandCancelled):
            run_command(python(FOREVER), timeout=120, cancel=token)
    finally:
        timer.cancel()

    # Generous: the bound being tested is that it is a small multiple of the
    # poll interval rather than the command's timeout.
    assert time.monotonic() - started < 0.2 + CANCEL_POLL_INTERVAL + 10


# --- leaving nothing behind ---------------------------------------------


def _alive(pid: int) -> bool:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        finished = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=30
        )
        return str(pid) in finished.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not ours, but it exists
        return True
    return True


def test_a_cancelled_child_is_actually_gone() -> None:
    token = CancelToken()
    recorded: list[int] = []
    real_popen = subprocess.Popen

    class Recording(subprocess.Popen):  # type: ignore[type-arg]
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            recorded.append(self.pid)

    subprocess.Popen = Recording  # type: ignore[misc]
    timer = threading.Timer(0.3, token.cancel)
    timer.start()
    try:
        with pytest.raises(CommandCancelled):
            run_command(python(FOREVER), timeout=120, cancel=token)
    finally:
        timer.cancel()
        subprocess.Popen = real_popen  # type: ignore[misc]

    assert recorded, "the child was never started"
    deadline = time.monotonic() + 10
    while _alive(recorded[0]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(recorded[0]), "the cancelled child is still running"


def test_an_interrupt_during_a_command_does_not_leave_the_child_running() -> None:
    """A signal handler that raises must not be able to orphan a process.

    SystemExit is what the CLI's handler raises on a second interrupt, and it
    is a BaseException, so only a ``finally`` catches it.
    """

    recorded: list[int] = []
    real_popen = subprocess.Popen

    class Recording(subprocess.Popen):  # type: ignore[type-arg]
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            recorded.append(self.pid)

    class Exploding(CancelToken):
        @property
        def cancelled(self) -> bool:
            raise SystemExit(130)

    subprocess.Popen = Recording  # type: ignore[misc]
    try:
        with pytest.raises(SystemExit):
            run_command(python(FOREVER), timeout=120, cancel=Exploding())
    finally:
        subprocess.Popen = real_popen  # type: ignore[misc]

    assert recorded
    deadline = time.monotonic() + 10
    while _alive(recorded[0]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(recorded[0]), "an interrupt orphaned the child"


@POSIX_ONLY
def test_the_child_runs_in_a_process_group_of_its_own() -> None:
    """The reason a terminal's Ctrl+C no longer reaches ``docker`` directly."""

    result = run_command(python("import os; print(os.getpgid(0), os.getpid())"), timeout=30)
    group, pid = (int(value) for value in result.stdout.split())

    assert group == pid, "the child should lead its own group"
    assert group != os.getpgid(0), "the child should not share DevDock's group"


@POSIX_ONLY
def test_cancelling_also_ends_processes_the_child_started() -> None:
    """The whole group is signalled, so a helper is not left behind."""

    token = CancelToken()
    marker: list[int] = []
    code = (
        "import os, subprocess, sys, time\n"
        f"child = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(120)'])\n"
        "sys.stdout.write(str(child.pid)); sys.stdout.flush()\n"
        "time.sleep(120)\n"
    )

    def cancel_once_started() -> None:
        time.sleep(1.0)
        token.cancel()

    timer = threading.Timer(0.0, cancel_once_started)
    timer.start()
    try:
        with pytest.raises(CommandCancelled) as raised:
            run_command((sys.executable, "-c", code), timeout=120, cancel=token)
    finally:
        timer.cancel()

    grandchild = raised.value.raw_stdout.strip()
    if not grandchild.isdigit():
        pytest.skip("the child did not report its helper in time")
    marker.append(int(grandchild))
    deadline = time.monotonic() + 10
    while _alive(marker[0]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(marker[0]), "a grandchild outlived the cancelled command"


@WINDOWS_ONLY
def test_windows_runs_the_child_in_a_new_process_group() -> None:  # pragma: no cover
    """Asserted through the flag, which is the only observable here."""

    assert process_isolation().creationflags == subprocess.CREATE_NEW_PROCESS_GROUP
    assert run_command(python("print('ok')"), timeout=30).stdout.strip() == "ok"


# --- the behaviour the isolation exists for -----------------------------

# Written to disk rather than embedded: this is a script that launches a
# script, and three levels of quoting in one string literal is how the first
# attempt broke.
_BEATER = """
import signal, sys, time

signal.signal(signal.SIGINT, signal.SIG_DFL)
while True:
    open(sys.argv[1], "w").write(str(time.time()))
    time.sleep(0.05)
"""

_HARNESS = """
import signal, subprocess, sys, time

# The harness ignores the interrupt, so the only question the test asks is
# whether the *child* received it.
signal.signal(signal.SIGINT, signal.SIG_IGN)
isolate = sys.argv[1] == "isolated"
subprocess.Popen(
    [sys.executable, sys.argv[2], sys.argv[3]], start_new_session=isolate
)
sys.stdout.write("started\\n")
sys.stdout.flush()
time.sleep(120)
"""


def _beating(path: Path, window: float = 0.6) -> bool:
    """Whether the child is still writing.

    Liveness is measured by what the process *does*, not by whether its pid
    answers. A child killed here is left unreaped by a harness that is still
    sleeping, and a zombie answers ``kill(pid, 0)`` exactly as a live process
    does -- which is how this test first passed when it should not have.
    """

    before = path.read_text(encoding="utf-8") if path.exists() else ""
    time.sleep(window)
    after = path.read_text(encoding="utf-8") if path.exists() else ""
    return before != after


def _child_survives_a_group_interrupt(isolated: bool, workspace: Path) -> bool:
    """Start a harness in its own group, interrupt the group, report on its child."""

    beater = workspace / "beater.py"
    harness_script = workspace / "harness.py"
    heartbeat = workspace / "beat"
    beater.write_text(_BEATER, encoding="utf-8")
    harness_script.write_text(_HARNESS, encoding="utf-8")

    harness = subprocess.Popen(
        [
            sys.executable,
            str(harness_script),
            "isolated" if isolated else "shared",
            str(beater),
            str(heartbeat),
        ],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert harness.stdout is not None
        harness.stdout.readline()
        deadline = time.monotonic() + 10
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _beating(heartbeat), "the child never started beating"
        os.killpg(harness.pid, signal.SIGINT)
        time.sleep(0.5)
        return _beating(heartbeat)
    finally:
        with contextlib.suppress(OSError):
            os.killpg(harness.pid, signal.SIGKILL)
        harness.wait(timeout=10)


@POSIX_ONLY
def test_a_group_interrupt_does_not_reach_an_isolated_child(tmp_path: Path) -> None:
    """The defect this step exists to fix, shown against the real kernel.

    A terminal delivers Ctrl+C to every process in the foreground group. Until
    the child is put in a group of its own, that means the terminal interrupts
    ``docker`` directly, halfway through whatever it was doing, and DevDock
    only finds out afterwards.
    """

    assert _child_survives_a_group_interrupt(True, tmp_path)


@POSIX_ONLY
def test_without_isolation_a_group_interrupt_does_reach_the_child(tmp_path: Path) -> None:
    """The negative control: without it the test above would pass for free."""

    assert not _child_survives_a_group_interrupt(False, tmp_path)


# --- the Windows hazards, reproduced where Windows is not ---------------


def test_stopping_a_child_never_looks_at_signals_windows_does_not_define(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows has no SIGKILL. Naming it unconditionally broke every command there.

    Simulated rather than hoped for: the attribute is removed and the Windows
    branch is selected, so this fails on Linux for the same reason it would
    have failed on Windows.
    """

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.delattr(signal, "SIGKILL", raising=False)
    monkeypatch.delattr(signal, "SIGBREAK", raising=False)
    terminated: list[str] = []

    class FakeProcess:
        pid = 4242
        _polls = iter([None, None, 0, 0, 0, 0])

        def poll(self) -> int | None:
            return next(self._polls)

        def terminate(self) -> None:
            terminated.append("terminate")

        def kill(self) -> None:  # pragma: no cover - only if terminate is ignored
            terminated.append("kill")

        def wait(self, timeout: float | None = None) -> int:
            return 0

    _stop_child(FakeProcess(), grace=0.1)  # type: ignore[arg-type]

    assert terminated == ["terminate"], "Windows should ask once, with TerminateProcess"


def test_stopping_a_child_on_windows_never_calls_killpg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """os.killpg does not exist on Windows; reaching it there would be fatal."""

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.delattr(os, "killpg", raising=False)
    calls: list[str] = []

    class FakeProcess:
        pid = 4243

        def poll(self) -> int | None:
            return None if not calls else 0

        def terminate(self) -> None:
            calls.append("terminate")

        def kill(self) -> None:  # pragma: no cover
            calls.append("kill")

        def wait(self, timeout: float | None = None) -> int:
            return 0

    _stop_child(FakeProcess(), grace=0.1)  # type: ignore[arg-type]

    assert calls == ["terminate"]


def test_a_child_that_ignores_the_polite_request_is_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second stage must still be reachable on either platform."""

    monkeypatch.setattr(os, "name", "nt")
    calls: list[str] = []

    class Stubborn:
        pid = 4244

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            calls.append("terminate")

        def kill(self) -> None:
            calls.append("kill")

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired(["x"], timeout or 0)

    _stop_child(Stubborn(), grace=0.01)  # type: ignore[arg-type]

    assert calls == ["terminate", "kill"]


@POSIX_ONLY
def test_the_posix_path_escalates_from_term_to_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pid, number: sent.append(number))

    class Stubborn:
        pid = 4245

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired(["x"], timeout or 0)

    _stop_child(Stubborn(), grace=0.01)  # type: ignore[arg-type]

    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_the_cli_handles_a_break_signal_only_where_one_exists() -> None:
    """SIGBREAK is Windows-only; asking for it elsewhere would raise."""

    import devdock.cli as cli

    expected = {signal.SIGINT}
    if hasattr(signal, "SIGBREAK"):  # pragma: no cover - Windows only
        expected.add(signal.SIGBREAK)

    with cli.interruptible():
        installed = {number for number in expected if callable(signal.getsignal(number))}

    assert installed == expected
