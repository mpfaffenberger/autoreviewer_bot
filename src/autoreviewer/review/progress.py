"""What a review is doing right now, for the check run while it runs and as a trail afterwards.

`Progress` is a plain record with no I/O: agents feed it through `watch()`, a capability that sees every
model response (thinking, text, tool calls) without changing the run, and the job publishes `render()`
to GitHub on its own schedule. Model text is untrusted, so everything rendered is sanitized and sits in
code blocks, where markdown and HTML do nothing.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models import ModelRequestContext

from autoreviewer.review.render import sanitize

MAX_ACTIVITY = 40
"""Recent events shown, newest first."""
THINKING_TAIL = 800
ARG_CHARS = 80
TEXT_CHARS = 300
MAX_DETAILS = 60_000
"""GitHub caps a check run's text at 65,535 characters."""


@dataclass
class Progress:
    phase: str = "Starting"
    requests: int = 0
    tool_calls: int = 0
    thinking: str = ""
    """The tail of the latest thinking, from whichever agent thought last."""
    activity: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_ACTIVITY))
    started: float = field(default_factory=time.monotonic)
    version: int = 0
    """Bumped on every change, so a publisher can tell whether there's anything new to send."""

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self.note(phase)

    def note(self, event: str) -> None:
        self.activity.appendleft(event)
        self.version += 1

    def record(self, who: str, response: ModelResponse) -> None:
        self.requests += 1
        for part in response.parts:
            if isinstance(part, ThinkingPart) and part.content.strip():
                self.thinking = f"{who}: …{part.content.strip()[-THINKING_TAIL:]}"
            elif isinstance(part, TextPart) and part.content.strip():
                self.note(f"{who} says: {_clip(part.content, TEXT_CHARS)}")
            elif isinstance(part, ToolCallPart):
                self.tool_calls += 1
                self.note(f"{who} → {_call(part)}")
        self.version += 1

    def watch(self, who: str) -> AbstractCapability[None]:
        """A capability that records each of an agent's model responses here, labelled `who`."""
        return _Watch(self, who)

    def summary(self) -> str:
        minutes, seconds = divmod(int(time.monotonic() - self.started), 60)
        return f"{self.phase}: {self.requests} model requests, {self.tool_calls} tool calls, {minutes}m{seconds:02d}s."

    def details(self) -> str:
        parts = []
        if self.thinking:
            parts.append(f"### Latest thinking\n\n{_block(self.thinking)}")
        if self.activity:
            parts.append(f"### Recent activity (newest first)\n\n{_block(chr(10).join(self.activity))}")
        return "\n\n".join(parts)[:MAX_DETAILS]


@dataclass
class _Watch(AbstractCapability[None]):
    progress: Progress
    who: str

    async def after_model_request(
        self, ctx: RunContext[None], *, request_context: ModelRequestContext, response: ModelResponse
    ) -> ModelResponse:
        self.progress.record(self.who, response)
        return response


def _call(part: ToolCallPart) -> str:
    if part.tool_name == "final_result":
        return "final report"
    args = ", ".join(f"{name}={_clip(repr(value), ARG_CHARS)}" for name, value in part.args_as_dict().items())
    return f"{part.tool_name}({args})"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _block(text: str) -> str:
    """A fenced block that model text can't break out of (a ``` inside would close the fence)."""
    return "```text\n" + sanitize(text, limit=MAX_DETAILS).replace("```", "``\u200b`") + "\n```"
