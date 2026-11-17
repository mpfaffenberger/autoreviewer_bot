"""Logfire tracing: every pydantic-ai agent run (model requests, tool calls, tokens), webhook requests,
and log records. Nothing is sent until LOGFIRE_TOKEN is set, so it's safe to run without one.

Logfire reads its standard environment variables too (LOGFIRE_TOKEN, LOGFIRE_ENVIRONMENT, ...).
"""

from __future__ import annotations

from importlib.metadata import version

import logfire


def configure(*, service: str, include_content: bool) -> None:
    """Call once at startup, before any agent runs.

    `include_content` records prompts, model output, and tool arguments and results: the reviewed code
    itself. Turn it off before reviewing code that mustn't leave this machine.
    """
    logfire.configure(
        service_name=service,
        service_version=version("autoreviewer"),
        send_to_logfire="if-token-present",
        console=False,  # stdout already gets our own log lines
        inspect_arguments=False,  # we never log f-strings through logfire; skip its source introspection
    )
    logfire.instrument_pydantic_ai(include_content=include_content)
