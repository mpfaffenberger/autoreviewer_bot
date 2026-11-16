"""Credentials never reach the logs."""

from __future__ import annotations

import logging

import pytest

from autoreviewer.logs import RedactSecrets, configure


def test_token_query_values_are_scrubbed_from_any_record() -> None:
    record = logging.LogRecord(
        "httpx2", logging.INFO, __file__, 1,
        "HTTP Request: GET %s", ("https://codeload.github.com/o/r/legacy.tar.gz/sha?token=ABCSECRET123&x=1",), None,
    )  # fmt: skip
    RedactSecrets().filter(record)
    assert "ABCSECRET123" not in record.getMessage()
    assert "?token=[redacted]&x=1" in record.getMessage()


def test_http_client_loggers_are_quiet_and_redaction_is_installed(caplog: pytest.LogCaptureFixture) -> None:
    configure()
    assert logging.getLogger("httpx2").getEffectiveLevel() == logging.WARNING
    assert any(isinstance(f, RedactSecrets) for h in logging.getLogger().handlers for f in h.filters)
