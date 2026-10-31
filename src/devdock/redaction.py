"""Best-effort suppression of secret values in captured runtime output.

Docker Compose interpolates variables into the configuration it echoes back,
so its output can repeat values DevDock was given. Before any captured output
reaches a terminal, every sufficiently distinctive value from DevDock's own
environment is replaced by the variable name that held it, and credentials
embedded in URLs are stripped.

This is a mitigation, not a guarantee. Compose also reads the project's `.env`
file, which DevDock deliberately never opens, so values that exist only there
cannot be recognized here. Two further layers narrow that gap: output for a
recognized failure is not shown at all, because the caller already has a
precise explanation without it, and values *shaped* like well-known
credentials are replaced even when DevDock has never seen them.
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

# Redaction scans at most this much text. Captured output is untrusted and can
# be arbitrarily large; past this point it is truncated rather than scanned.
MAX_REDACTABLE_CHARACTERS = 256_000
TRUNCATION_NOTICE = "\n[output truncated by DevDock before redaction]"

# C0/C1 control characters and DEL, except horizontal tab. These include the
# ESC that starts ANSI sequences, so stripping them neutralizes terminal
# escape injection from untrusted names and labels.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")

# scheme://user:password@host -> the password component only. Every quantifier
# is bounded: an unbounded scheme run made this quadratic, so a long line of
# plain text could stall redaction for over a minute.
_URL_CREDENTIALS = re.compile(
    r"(?P<prefix>[a-zA-Z][a-zA-Z0-9+.-]{0,30}://[^\s:/@]{1,255}:)[^\s@]{1,255}@"
)

# Values recognizable as credentials by shape alone. These catch secrets that
# reach Compose from the project's .env or the Compose file itself, which
# DevDock never reads and so cannot match by value. Every pattern is anchored
# and bounded: no nested quantifiers, so hostile input cannot cause
# catastrophic backtracking.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[^-]{0,8000}"
            r"-----END [A-Z ]{0,40}PRIVATE KEY-----"
        ),
        "[redacted private key]",
    ),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
        "[redacted jwt]",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted aws key id]"),
    (re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,255}"), "[redacted github token]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,255}"), "[redacted slack token]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,255}"), "[redacted api key]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer [redacted]"),
)

# key=value and key: value pairs whose key names a secret. The value is
# replaced; the key is kept so the message still explains what went wrong.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_-]{0,64}"
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|credential))"
    r"(\s*[=:]\s*)"
    r"(\"[^\"\n]{1,255}\"|'[^'\n]{1,255}'|[^\s,;]{1,255})"
)


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
    """Replace known environment values, credential shapes, and URL passwords.

    Environment values are matched in one pass with longest-first alternation,
    so a short value can never rewrite the ``${NAME}`` placeholder a longer one
    produced. Shape-based patterns run afterwards and catch credentials DevDock
    has never seen, including values Compose read from the project's ``.env``.
    """

    if not text:
        return text
    if len(text) > MAX_REDACTABLE_CHARACTERS:
        text = text[:MAX_REDACTABLE_CHARACTERS] + TRUNCATION_NOTICE
    candidates = _redactable(environ)
    if candidates:
        names_by_value = {value: name for value, name in candidates}
        pattern = re.compile("|".join(re.escape(value) for value, _ in candidates))
        text = pattern.sub(lambda match: "${" + names_by_value[match.group(0)] + "}", text)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    text = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{PLACEHOLDER}", text
    )
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
