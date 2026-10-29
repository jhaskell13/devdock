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
