"""Logging setup. Credentials must never reach the logs, whichever library writes the line.

githubkit's HTTP client logs request URLs at INFO, and GitHub's tarball download redirects to a
codeload URL carrying `?token=...` (seen in the M1 live test). So: HTTP client loggers stay at
WARNING, and every record is scrubbed of token-like query values as a backstop.
"""

from __future__ import annotations

import logging
import re

import logfire

_SECRET_QUERY = re.compile(r"([?&](?:token|access_token|code|client_secret|jwt)=)[^&\s\"']+", re.IGNORECASE)
_NOISY = ("httpx", "httpx2", "httpcore", "openai")


class RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        scrubbed = _SECRET_QUERY.sub(r"\1[redacted]", message)
        if scrubbed != message:
            record.msg, record.args = scrubbed, None
        return True


def configure(level: int = logging.INFO, *, to_logfire: bool = False) -> None:
    """Log to stderr, and to Logfire as well with `to_logfire` (after telemetry.configure)."""
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handlers: list[logging.Handler] = [console]
    if to_logfire:
        handlers.append(logfire.LogfireLoggingHandler())
    for handler in handlers:  # the same scrubbing on every way out
        handler.addFilter(RedactSecrets())
    logging.basicConfig(level=level, handlers=handlers, force=True)
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
