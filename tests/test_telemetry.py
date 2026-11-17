"""Telemetry: logs reach Logfire scrubbed, and nothing is sent without a token."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator

import logfire
import pytest
from logfire.testing import CaptureLogfire

from autoreviewer import logs, telemetry
from autoreviewer.web.config import parse_bool


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:], root.level = handlers, level


def test_logs_reach_logfire_with_tokens_scrubbed(capfire: CaptureLogfire, restore_logging: None) -> None:
    """Through the Logfire handler alone: the console handler's filter rewrites records in place, so going
    through the logger would pass even if the Logfire handler had no filter of its own."""
    logs.configure(to_logfire=True)
    [to_logfire] = [h for h in logging.getLogger().handlers if isinstance(h, logfire.LogfireLoggingHandler)]
    record = logging.LogRecord(
        "githubkit",
        logging.WARNING,
        __file__,
        1,
        "GET https://codeload.github.com/o/r/tar?token=ghs_supersecret",
        None,
        None,
    )
    to_logfire.handle(record)
    [span] = [s for s in capfire.exporter.exported_spans_as_dict() if "codeload" in str(s["attributes"])]
    message = str(span["attributes"]["logfire.msg"])
    assert "ghs_supersecret" not in message and "token=[redacted]" in message


@pytest.mark.parametrize("include_content", [True, False])
def test_configure_sends_only_with_a_token_and_honours_the_content_switch(
    monkeypatch: pytest.MonkeyPatch, include_content: bool
) -> None:
    calls: dict[str, dict[str, object]] = {}
    monkeypatch.setattr(logfire, "configure", lambda **kwargs: calls.setdefault("configure", kwargs))
    monkeypatch.setattr(logfire, "instrument_pydantic_ai", lambda **kwargs: calls.setdefault("instrument", kwargs))
    telemetry.configure(service="autoreviewer", include_content=include_content)
    assert calls["configure"]["send_to_logfire"] == "if-token-present"
    assert calls["instrument"] == {"include_content": include_content}


def test_a_traceback_carrying_a_token_reaches_logfire_scrubbed(capfire: CaptureLogfire, restore_logging: None) -> None:
    logs.configure(to_logfire=True)
    [to_logfire] = [h for h in logging.getLogger().handlers if isinstance(h, logfire.LogfireLoggingHandler)]
    try:
        raise RuntimeError("redirected to https://codeload.github.com/o/r/tar?token=ghs_supersecret")
    except RuntimeError:
        record = logging.LogRecord("job", logging.ERROR, __file__, 1, "review failed", None, sys.exc_info())
    to_logfire.handle(record)
    exported = str(capfire.exporter.exported_spans_as_dict())
    assert "ghs_supersecret" not in exported and "token=[redacted]" in exported


@pytest.mark.parametrize(
    ("value", "expected"), [(None, True), ("", True), ("false", False), ("0", False), ("Off", False), ("yes", True)]
)
def test_the_content_switch_parses_strictly(value: str | None, expected: bool) -> None:
    env = {} if value is None else {"X": value}
    assert parse_bool(env, "X", default=True) is expected


def test_a_typo_in_the_content_switch_stops_startup() -> None:
    with pytest.raises(ValueError, match="expected true/false"):
        parse_bool({"X": "flase"}, "X", default=True)
