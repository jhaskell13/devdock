"""Best-effort suppression of secret values in captured runtime output.

Docker Compose interpolates variables into the configuration it echoes back,
so its output can repeat values DevDock was given. Before any captured output
reaches a terminal, every sufficiently distinctive value from DevDock's own
environment is replaced by the variable name that held it, and credentials
embedded in URLs are stripped.

This is a mitigation, not a guarantee. Compose also reads the project's `.env`
file, which DevDock deliberately never opens, so values that exist only there
cannot be recognized here. That gap is why only a short, redacted tail of
output is shown by default and the full output requires an explicit flag.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

MIN_REDACTABLE_LENGTH = 4
MAX_SUMMARY_LINES = 20
PLACEHOLDER = "[redacted]"

# Well-known variables whose values are not secrets and whose redaction would
# only make output harder to read. Anything not listed here is redacted.
NON_SECRET_NAMES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "TERM",
        "COLORTERM",
        "LANG",
        "LANGUAGE",
        "SHELL",
        "PWD",
        "OLDPWD",
        "SHLVL",
        "DISPLAY",
        "EDITOR",
        "VISUAL",
        "PAGER",
        "HOSTNAME",
        "TZ",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "COMSPEC",
        "SYSTEMROOT",
        "WINDIR",
        "OS",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "COMMONPROGRAMFILES",
        "COMPOSE_ANSI",
        "COMPOSE_PROGRESS",
        "COMPOSE_REMOVE_ORPHANS",
        "DOCKER_CLI_HINTS",
    }
)
_NON_SECRET_PREFIXES = ("LC_", "XDG_")

# C0/C1 control characters and DEL, except horizontal tab. These include the
# ESC that starts ANSI sequences, so stripping them neutralizes terminal
# escape injection from untrusted names and labels.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")

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
        and name.upper() not in NON_SECRET_NAMES
        and not name.upper().startswith(_NON_SECRET_PREFIXES)
    ]
    candidates.sort(key=lambda pair: len(pair[0]), reverse=True)
    return candidates


def redact(text: str, environ: Mapping[str, str]) -> str:
    """Replace known environment values and URL passwords in ``text``.

    All values are matched in one pass with longest-first alternation, so a
    short value can never rewrite the ``${NAME}`` placeholder a longer one
    produced.
    """

    if not text:
        return text
    candidates = _redactable(environ)
    if candidates:
        names_by_value = {value: name for value, name in candidates}
        pattern = re.compile("|".join(re.escape(value) for value, _ in candidates))
        text = pattern.sub(lambda match: "${" + names_by_value[match.group(0)] + "}", text)
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


def sanitize(text: str) -> str:
    """Neutralize control characters so untrusted strings are safe to print.

    Container names, labels, and runtime JSON fields come from the Docker
    daemon and can be set by any local tool. A control character in one of
    them could rewrite or hide terminal output, so they are replaced with
    ``?`` before display. Tabs are kept for alignment.
    """

    return _CONTROL_CHARACTERS.sub("?", text)
