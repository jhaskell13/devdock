"""Colour, spacing, and the rule that only DevDock's own text may be styled."""

from __future__ import annotations

import io
import re

import pytest

from devdock.render import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    ColorChoice,
    Style,
    choose_color,
    spaced,
)
from devdock.status import READINESS_LABELS

ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
PALETTE = {RESET, BOLD, DIM, RED, GREEN, YELLOW, CYAN}

PLAIN = Style(color=False)
COLOR = Style(color=True)


class FakeStream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


# --- when to colour -----------------------------------------------------


@pytest.mark.parametrize(
    ("choice", "tty", "environ", "expected"),
    [
        ("auto", True, {}, True),
        ("auto", False, {}, False),
        ("always", False, {}, True),
        ("never", True, {}, False),
        ("auto", True, {"NO_COLOR": "1"}, False),
        ("auto", False, {"FORCE_COLOR": "1"}, True),
        ("auto", False, {"FORCE_COLOR": "0"}, False),
        ("auto", False, {"FORCE_COLOR": ""}, False),
        ("auto", True, {"TERM": "dumb"}, False),
        ("auto", True, {"TERM": "xterm-256color"}, True),
    ],
)
def test_colour_follows_the_conventions_users_already_set(
    choice: ColorChoice, tty: bool, environ: dict[str, str], expected: bool
) -> None:
    assert choose_color(choice, FakeStream(tty), environ) is expected


def test_no_color_is_honoured_even_against_force_color() -> None:
    """It is the one a user sets once and expects everything to respect."""

    assert choose_color("auto", FakeStream(True), {"NO_COLOR": "1", "FORCE_COLOR": "1"}) is False


def test_an_explicit_choice_beats_detection() -> None:
    assert choose_color("always", FakeStream(False), {"TERM": "dumb"}) is True
    assert choose_color("never", FakeStream(True), {"FORCE_COLOR": "1"}) is False


def test_a_stream_that_cannot_say_is_treated_as_nobody_looking() -> None:
    class Awkward:
        def isatty(self) -> bool:
            raise ValueError("closed")

    assert choose_color("auto", Awkward(), {}) is False  # type: ignore[arg-type]


# --- what gets styled ---------------------------------------------------


def test_nothing_is_changed_when_colour_is_off() -> None:
    for line in ("[OK] UP_READY: done", "  web: ready", "SERVICE  CONTAINER  STATE"):
        assert PLAIN.line(line) == line


@pytest.mark.parametrize(
    ("line", "color"),
    [
        ("[OK] UP_READY: all ready.", GREEN),
        ("[PASS] PROFILE_VALID: fine.", GREEN),
        ("[REFUSED] UP_FAILED: no.", RED),
        ("[FAIL] PORT_UNAVAILABLE: taken.", RED),
        ("[WARN] GRAPH_UNAVAILABLE: unknown.", YELLOW),
        ("[ADVISORY] PORT_UNAVAILABLE: taken.", YELLOW),
    ],
)
def test_a_severity_marker_is_coloured_by_what_it_means(line: str, color: str) -> None:
    assert COLOR.line(line).startswith(f"{BOLD}{color}")


def test_readiness_labels_are_coloured_by_what_they_mean() -> None:
    assert GREEN in COLOR.line("web      running  healthy  ready      -")
    assert RED in COLOR.line("db       running  unhealthy  unhealthy  -")
    assert YELLOW in COLOR.line("api      running  -        starting   -")


def test_running_unverified_is_not_mistaken_for_ready() -> None:
    """The longer label has to win, or an unverified service looks verified."""

    styled = COLOR.line("worker  c  running  -  running (readiness unverified)  -")

    assert YELLOW in styled
    assert f"{GREEN}ready" not in styled


def test_every_readiness_label_has_a_colour() -> None:
    """A label with none would print plain, which nobody notices for a year."""

    for label in READINESS_LABELS.values():
        assert ESCAPE.search(COLOR.line(f"svc  c  running  -  {label}  -")), label


def test_colour_does_not_change_the_visible_width() -> None:
    """The status table is padded before this runs, so alignment must survive."""

    row = "web      devdock-x-web-1   running  healthy  ready    127.0.0.1:80->80/tcp"

    assert ESCAPE.sub("", COLOR.line(row)) == row


def test_supporting_detail_is_dimmed_rather_than_shouted() -> None:
    assert COLOR.line("       Evidence: Path: /p/compose.yaml").startswith(DIM)
    assert COLOR.line("DevDock acts only on resources that Compose labeled").startswith(DIM)


def test_a_label_is_coloured_and_its_value_is_not() -> None:
    styled = COLOR.line("Project root: /home/someone/work")

    assert styled.startswith(f"{CYAN}Project root{RESET}")
    assert "/home/someone/work" in ESCAPE.sub("", styled)


def test_blank_lines_are_left_alone() -> None:
    assert COLOR.line("") == ""
    assert COLOR.line("   ") == "   "


def test_only_the_palette_is_ever_emitted() -> None:
    """Every escape in the output is one of the few this module defines."""

    lines = [
        "[FAIL] UP_FAILED: no.",
        "  web: ready after 1.0s",
        "       Evidence: something",
        "SERVICE  CONTAINER  STATE  HEALTH  READINESS  PORTS",
        "Suggested action: do the thing",
    ]
    for line in lines:
        assert set(ESCAPE.findall(COLOR.line(line))) <= PALETTE, line


# --- spacing ------------------------------------------------------------


def test_a_blank_line_is_inserted_before_each_group() -> None:
    assert spaced(("Project root: /p", "Startup order (2 layers):", "  1. db")) == (
        "Project root: /p",
        "",
        "Startup order (2 layers):",
        "  1. db",
    )


def test_returning_to_the_left_margin_starts_a_group() -> None:
    """A summary or a closing note always does this."""

    assert spaced(("Dependencies (1):", "  web depends on db", "Waited 2.0s.")) == (
        "Dependencies (1):",
        "  web depends on db",
        "",
        "Waited 2.0s.",
    )


def test_spacing_adds_nothing_at_the_start_and_never_doubles_up() -> None:
    assert spaced(("Startup order (1 layer):", "  1. db")) == (
        "Startup order (1 layer):",
        "  1. db",
    )
    assert spaced(("", "Summary:")) == ("", "Summary:")


def test_spacing_removes_and_reorders_nothing() -> None:
    lines = ("a:", "  b", "c", "  d", "Summary: e")

    assert [line for line in spaced(lines) if line] == list(lines)
