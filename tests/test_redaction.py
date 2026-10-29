import time

import pytest

from devdock.redaction import MAX_SUMMARY_LINES, redact, summarize


def test_environment_values_are_replaced_by_their_names() -> None:
    environ = {"DB_PASSWORD": "hunter2-very-secret", "API_TOKEN": "tok_abcdef123456"}

    text = redact("connecting with hunter2-very-secret and tok_abcdef123456", environ)

    assert "hunter2-very-secret" not in text
    assert "tok_abcdef123456" not in text
    assert "${DB_PASSWORD}" in text and "${API_TOKEN}" in text


def test_longer_values_are_redacted_before_shorter_substrings() -> None:
    environ = {"SHORT": "secret", "LONG": "secret-extended-value"}

    text = redact("using secret-extended-value here", environ)

    assert text == "using ${LONG} here"


def test_short_values_are_left_alone_to_avoid_corrupting_output() -> None:
    environ = {"DEBUG": "1", "MODE": "on", "TIER": "dev"}

    text = redact("service dev started on port 1", environ)

    assert text == "service dev started on port 1"


def test_url_passwords_are_stripped_even_when_unknown() -> None:
    text = redact("dsn postgres://admin:s3cr3t-from-dotenv@db:5432/app", {})

    assert "s3cr3t-from-dotenv" not in text
    assert "postgres://admin:[redacted]@db:5432/app" in text


def test_redaction_handles_empty_text_and_empty_environment() -> None:
    assert redact("", {"TOKEN": "abcd"}) == ""
    assert redact("plain output", {}) == "plain output"


def test_whitespace_only_environment_values_do_not_blank_the_output() -> None:
    text = redact("a   b\tc", {"PAD": "    ", "TAB": "\t\t\t\t"})

    assert text == "a   b\tc"


def test_summary_keeps_the_last_lines_and_reports_how_many_were_hidden() -> None:
    body = "\n".join(f"line {index}" for index in range(MAX_SUMMARY_LINES + 5))

    lines = summarize(body, {})

    assert len(lines) == MAX_SUMMARY_LINES + 1
    assert "5 earlier line(s) hidden" in lines[0]
    assert lines[-1] == f"line {MAX_SUMMARY_LINES + 4}"


def test_summary_drops_blank_lines_and_redacts() -> None:
    lines = summarize("\n\nfailed with hunter2-very-secret\n\n", {"PW": "hunter2-very-secret"})

    assert lines == ("failed with ${PW}",)


def test_sanitize_strips_escapes_and_controls_but_keeps_tabs() -> None:
    from devdock.redaction import sanitize

    assert sanitize("a\tb\x1b[31mred\x00\x7f\x9b") == "a\tb?[31mred???"
    assert sanitize("plain") == "plain"


def test_short_values_cannot_mangle_placeholders_from_longer_ones() -> None:
    environ = {"DB_PASSWORD": "hunter2-very-secret", "X": "PASS"}

    text = redact("pw=hunter2-very-secret", environ)

    assert text == "pw=${DB_PASSWORD}"


def test_well_known_non_secret_variables_are_not_redacted() -> None:
    environ = {
        "PATH": "/usr/bin:/bin",
        "TERM": "xterm-256color",
        "LC_ALL": "en_US.UTF-8",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "API_KEY": "sk-live-123456",
    }

    text = redact("xterm-256color en_US.UTF-8 /usr/bin:/bin sk-live-123456", environ)

    assert text == "xterm-256color en_US.UTF-8 /usr/bin:/bin ${API_KEY}"


def test_home_and_user_are_still_redacted_as_private() -> None:
    text = redact("/home/jane/project", {"HOME": "/home/jane", "USER": "jane"})

    assert text == "${HOME}/project"


@pytest.mark.parametrize(
    ("secret", "expected"),
    [
        ("AKIAIOSFODNN7EXAMPLE", "[redacted aws key id]"),
        ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "[redacted github token]"),
        ("xoxb-123456789012-abcdefghijkl", "[redacted slack token]"),
        ("sk-abcdefghijklmnopqrstuvwxyz", "[redacted api key]"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u",
            "[redacted jwt]",
        ),
    ],
)
def test_credential_shapes_are_redacted_without_being_known(secret: str, expected: str) -> None:
    text = redact(f"auth failed using {secret} against registry", {})

    assert secret not in text
    assert expected in text


def test_private_key_blocks_are_removed_whole() -> None:
    key = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAx7Vn9s0PRIVATEKEYMATERIALHERE\n"
        "-----END RSA PRIVATE KEY-----"
    )

    text = redact(f"could not read\n{key}\nend", {})

    assert "PRIVATEKEYMATERIAL" not in text
    assert "[redacted private key]" in text


@pytest.mark.parametrize(
    "assignment",
    [
        "POSTGRES_PASSWORD=s3cr3t-from-dotenv",
        "api_key: s3cr3t-from-dotenv",
        'token="s3cr3t-from-dotenv"',
        "ACCESS-KEY = s3cr3t-from-dotenv",
    ],
)
def test_secret_assignments_keep_the_key_and_drop_the_value(assignment: str) -> None:
    text = redact(f"env {assignment} rejected", {})

    assert "s3cr3t-from-dotenv" not in text
    assert "[redacted]" in text
    assert assignment.split("=")[0].split(":")[0].strip() in text


def test_ordinary_text_with_secret_like_words_is_not_mangled() -> None:
    text = redact("the password was not supplied; see docs", {})

    assert text == "the password was not supplied; see docs"


def test_redaction_is_bounded_for_very_large_output() -> None:
    from devdock.redaction import MAX_REDACTABLE_CHARACTERS

    text = redact("a" * (MAX_REDACTABLE_CHARACTERS + 10_000), {})

    assert len(text) < MAX_REDACTABLE_CHARACTERS + 100
    assert "truncated by DevDock" in text


def test_redaction_stays_linear_on_hostile_input() -> None:
    """Guards against catastrophic backtracking in the credential patterns.

    An unbounded scheme run once made the URL pattern quadratic: 256 KB of
    plain text took over a minute, so any long Compose error could stall
    DevDock. These inputs are the shapes that triggered it.
    """

    from devdock.redaction import MAX_REDACTABLE_CHARACTERS

    hostile = [
        "a" * MAX_REDACTABLE_CHARACTERS,
        ("http" + "a" * 200 + " ") * 1000,
        ("x" * 50 + "://" + "y" * 50 + ":") * 2000,
        ("password=" + "z" * 300 + " ") * 500,
    ]

    start = time.perf_counter()
    for text in hostile:
        redact(text, {"TOKEN": "abcd-efgh-ijkl"})
    elapsed = time.perf_counter() - start

    assert elapsed < 5.0, f"redaction took {elapsed:.1f}s; a pattern is likely backtracking"
