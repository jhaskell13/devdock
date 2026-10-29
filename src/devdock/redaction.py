"""Best-effort suppression of secret values in captured runtime output.

Docker Compose interpolates variables into the configuration it echoes back,
so its output can repeat values DevDock was given. Before any captured output
reaches a terminal, every sufficiently distinctive value from DevDock's own
environment is replaced by the variable name that held it, and credentials
embedded in URLs are stripped.

This is a mitigation, not a guarantee. Compose also reads the project's `.env`
file, which DevDock deliberately never opens, so values that exist only there
cannot be recognized here. That gap is why captured output stays hidden unless
the user asks for it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

MIN_REDACTABLE_LENGTH = 4
MAX_SUMMARY_LINES = 20
PLACEHOLDER = "[redacted]"

# scheme://user:password@host -> the password component only.
_URL_CREDENTIALS = re.compile(r"(?P<prefix>[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)[^\s@]+@")


def _redactable(environ: Mapping[str, str]) -> list[tuple[str, str]]:
    """Return (value, name) pairs worth searching for, longest value first.

    Very short values are skipped: they collide with ordinary words and
    punctuation, and redacting them would corrupt output without protecting
    anything meaningful.
    """

    candidates = [
        (value.strip(), name)
        for name, value in environ.items()
        if len(value.strip()) >= MIN_REDACTABLE_LENGTH
    ]
    candidates.sort(key=lambda pair: len(pair[0]), reverse=True)
    return candidates


def redact(text: str, environ: Mapping[str, str]) -> str:
    """Replace known environment values and URL passwords in ``text``."""

    if not text:
        return text
    for value, name in _redactable(environ):
        if value in text:
            text = text.replace(value, "${" + name + "}")
    return _URL_CREDENTIALS.sub(lambda match: f"{match.group('prefix')}{PLACEHOLDER}@", text)


def summarize(
    text: str,
    environ: Mapping[str, str],
    *,
    max_lines: int = MAX_SUMMARY_LINES,
) -> tuple[str, ...]:
    """Return the last ``max_lines`` meaningful lines of ``text``, redacted."""

    lines = [line.rstrip() for line in redact(text, environ).splitlines() if line.strip()]
    if len(lines) <= max_lines:
        return tuple(lines)
    hidden = len(lines) - max_lines
    return (f"... {hidden} earlier line(s) hidden; use --show-output for all of it.",) + tuple(
        lines[-max_lines:]
    )
