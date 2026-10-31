"""Colour and spacing for terminal output.

This module is the only place DevDock emits an escape sequence, and it runs
*after* ``redaction.sanitize`` has stripped every control character out of the
text. That order is the whole safety argument: a container name or a label
value cannot carry its own styling, because by the time styling is applied
there are no escapes left in the string except the ones chosen here.

Styling is therefore decided from DevDock's own vocabulary -- the severity
markers it writes, the readiness labels it defines -- never from the content
around them. The worst a hostile name can do is contain the word ``ready`` and
be coloured green.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import IO, Literal

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"

ColorChoice = Literal["auto", "always", "never"]

# Severity markers DevDock writes at the start of a line, and the colour each
# one earns. The marker itself is matched, not the text around it.
_MARKERS: Mapping[str, str] = {
    "OK": GREEN,
    "PASS": GREEN,
    "REFUSED": RED,
    "FAIL": RED,
    "WARN": YELLOW,
    "ADVISORY": YELLOW,
    "SKIP": DIM,
}
_MARKER = re.compile(r"^(\s*)\[(" + "|".join(_MARKERS) + r")\]")

# A readiness label is coloured wherever it appears as a whole word. Escape
# sequences have no width, so the status table stays aligned even though the
# cells were padded before this ran.
_READINESS_COLORS: Mapping[str, str] = {
    "ready": GREEN,
    "exited cleanly": GREEN,
    "running (readiness unverified)": YELLOW,
    "starting": YELLOW,
    "unhealthy": RED,
    "stopped": RED,
    "missing": RED,
    "unknown": YELLOW,
    # Compose's own health words, which appear in the status table's HEALTH
    # column and in the source lines beneath it. Sorted longest-first below,
    # so "unhealthy" is never matched as "healthy".
    "healthy": GREEN,
}
# Longest first, so "running (readiness unverified)" is not matched as "ready".
_READINESS = re.compile(
    "|".join(re.escape(label) for label in sorted(_READINESS_COLORS, key=len, reverse=True))
)

# Lines that introduce a group. A blank line before each one is most of what
# turns a wall of output into something scannable.
_SECTION = re.compile(r"^\S.*:$")
_HEADLINE = re.compile(r"^\[(?:OK|REFUSED)\]")
_LABELLED = re.compile(r"^([A-Z][A-Za-z ]+):(\s)")
_TABLE_HEADER = re.compile(r"^SERVICE\s+CONTAINER\s")
_REMEDY = re.compile(r"^Suggested action:")
_EVIDENCE = re.compile(r"^(\s+)(Evidence|Suggested action):")
# Sentences that explain DevDock's own rules rather than report on the
# project. Useful to have, not useful to read twice.
_BOILERPLATE = ("DevDock acts only on resources", "Availability can change", "Output above is")


@dataclass(frozen=True)
class Style:
    """Whether to colour, decided once and passed down."""

    color: bool = False

    def paint(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.color and text else text

    def line(self, text: str) -> str:
        """Style one already-sanitized line of output."""

        if not self.color or not text.strip():
            return text
        if text.startswith(_BOILERPLATE) or text.lstrip().startswith(_BOILERPLATE):
            return self.paint(text, DIM)
        marker = _MARKER.match(text)
        if marker is not None:
            indent, name = marker.group(1), marker.group(2)
            rest = text[marker.end() :]
            painted = self.paint(f"[{name}]", BOLD + _MARKERS[name])
            if _HEADLINE.match(text):
                return f"{indent}{painted}{self.paint(rest, BOLD)}"
            return f"{indent}{painted}{self._readiness(rest)}"
        evidence = _EVIDENCE.match(text)
        if evidence is not None:
            return self.paint(text, DIM)
        if _TABLE_HEADER.match(text):
            return self.paint(text, BOLD)
        if _REMEDY.match(text):
            head, _, tail = text.partition(":")
            return f"{self.paint(head + ':', BOLD + YELLOW)}{tail}"
        labelled = _LABELLED.match(text)
        if labelled is not None:
            head = labelled.group(1)
            rest = text[labelled.end(1) :]
            return f"{self.paint(head, CYAN)}{self._readiness(rest)}"
        return self._readiness(text)

    def _readiness(self, text: str) -> str:
        return _READINESS.sub(
            lambda match: self.paint(match.group(0), _READINESS_COLORS[match.group(0)]), text
        )


def choose_color(choice: ColorChoice, stream: IO[str], environ: Mapping[str, str]) -> bool:
    """Decide whether to colour, following the conventions users already expect.

    ``NO_COLOR`` wins over everything because it is the one a user sets once
    and expects every tool to honour. An explicit ``--color`` beats detection,
    detection falls back to "is anyone looking at this", and a dumb terminal
    is treated as nobody.
    """

    if environ.get("NO_COLOR"):
        return False
    if choice == "always":
        return True
    if choice == "never":
        return False
    if environ.get("FORCE_COLOR") not in (None, "", "0"):
        return True
    if environ.get("TERM", "") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):  # a closed or unusual stream
        return False


def spaced(lines: tuple[str, ...]) -> tuple[str, ...]:
    """Insert a blank line before each group, so output can be scanned.

    Nothing is removed and nothing is reordered: a reader who pipes DevDock
    into a file sees the same lines they always did, with somewhere for the
    eye to rest.
    """

    spaced_lines: list[str] = []
    for line in lines:
        previous = spaced_lines[-1] if spaced_lines else ""
        # A group starts at a heading, at the status table, or wherever the
        # text returns to the left margin after an indented block -- which is
        # what a summary, a total, or a closing note always does.
        starts_group = (
            bool(_SECTION.match(line))
            or bool(_TABLE_HEADER.match(line))
            or (bool(line[:1].strip()) and previous.startswith(("  ", "\t")))
        )
        if starts_group and previous.strip():
            spaced_lines.append("")
        spaced_lines.append(line)
    return tuple(spaced_lines)
