"""Time limits for a review.

A soft deadline makes every lens wrap up: from its next step it may only return its final report, with an
instruction saying time is up, so it ends with what it found instead of being cut off mid-thought. A hard
deadline (enforced by the job, `asyncio.timeout`) cancels whatever is still running. A review cut short by
either never approves: it may still request changes for what it found.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import ToolDefinition

WRAP_UP = (
    "Time is up for this review. Stop exploring: return your final report now with what you have found so "
    "far, and list in `reviewed` only the files you actually inspected."
)


@dataclass
class Deadline:
    soft_s: float
    """Seconds after `started` when every lens must wrap up."""
    started: float = field(default_factory=time.monotonic)
    wrapped_up: set[str] = field(default_factory=set[str])
    """Labels of the lenses that were told to wrap up."""

    @property
    def passed(self) -> bool:
        return time.monotonic() - self.started >= self.soft_s

    def watch(self, label: str) -> AbstractCapability[None]:
        """A capability that makes the lens labelled `label` wrap up once the deadline has passed."""
        return _WrapUp(self, label)


@dataclass
class _WrapUp(AbstractCapability[None]):
    deadline: Deadline
    label: str

    async def prepare_tools(self, ctx: RunContext[None], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
        return [] if self.deadline.passed else tool_defs  # output tools aren't function tools: the report stays

    async def before_model_request(
        self, ctx: RunContext[None], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        if self.deadline.passed and self.label not in self.deadline.wrapped_up:
            self.deadline.wrapped_up.add(self.label)
            last = request_context.messages[-1]
            if isinstance(last, ModelRequest):
                last.parts = [*last.parts, UserPromptPart(WRAP_UP)]
        return request_context
