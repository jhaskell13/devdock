"""Documentation must keep pace with the codes the source emits."""

import ast
import re
from pathlib import Path

from devdock.failures import _SUFFIX

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "devdock"
CLI_DOC = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")

CODE_PREFIXES = (
    "PROFILE",
    "PROJECT",
    "PYTHON",
    "RUNTIME",
    "DOCKER",
    "COMPOSE",
    "ENVIRONMENT",
    "REQUIRED",
    "PORT",
    "UP",
    "STATUS",
    "DOWN",
)
LITERAL = re.compile(r"\b((?:" + "|".join(CODE_PREFIXES) + r")_[A-Z][A-Z_]+)\b")
GATE_SUFFIX = re.compile(r"^_[A-Z][A-Z_]+$")
# Upper-case identifiers in source that are environment variables, not codes.
NOT_CODES = {
    "COMPOSE_ANSI",
    "COMPOSE_PROGRESS",
    "COMPOSE_REMOVE_ORPHANS",
    "DOCKER_CLI_HINTS",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
}


def _string_constants(path: Path) -> list[str]:
    """Every string literal in a module, including the literal parts of f-strings."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def emitted_codes() -> set[str]:
    """Codes the source can emit: literal codes plus prefix-expanded gate suffixes.

    Only string literals are scanned, so Python constant names such as
    COMPOSE_NAMES are not mistaken for codes. The shared gate helper builds
    codes as f"{prefix}_SUFFIX"; its literal parts are expanded for each
    lifecycle prefix, as are the failure-taxonomy suffixes.
    """

    codes: set[str] = set()
    suffixes: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        if path.name in {"redaction.py", "failures.py"}:
            continue  # environment-variable names and taxonomy suffixes, expanded below
        for text in _string_constants(path):
            codes.update(LITERAL.findall(text))
            if GATE_SUFFIX.fullmatch(text):
                suffixes.add(text)
    for prefix in ("UP", "STATUS", "DOWN"):
        codes.update(f"{prefix}{suffix}" for suffix in suffixes)
    for prefix in ("UP", "DOWN"):
        codes.update(f"{prefix}_{suffix}" for suffix in _SUFFIX.values())
    return {code for code in codes if code not in NOT_CODES}


def documented(code: str) -> bool:
    if f"`{code}`" in CLI_DOC:
        return True
    prefix, _, suffix = code.partition("_")
    if prefix in {"UP", "STATUS", "DOWN"} and f"`_{suffix}`" in CLI_DOC:
        return True
    if code.startswith("REQUIRED_"):
        return f"`{code}`" in CLI_DOC
    return False


def test_every_emitted_code_is_documented_in_the_cli_contract() -> None:
    missing = sorted(code for code in emitted_codes() if not documented(code))

    assert not missing, f"codes emitted by src but absent from docs/cli.md: {missing}"


def test_the_code_table_does_not_list_codes_that_no_longer_exist() -> None:
    listed = set(re.findall(r"`((?:" + "|".join(CODE_PREFIXES) + r")_[A-Z_]+)`", CLI_DOC))
    stale = sorted(
        code
        for code in listed
        if code not in emitted_codes()
        and code not in {"DOCKER_HOST", "DOCKER_CONTEXT"}  # environment variables
    )

    assert not stale, f"codes documented but never emitted: {stale}"
