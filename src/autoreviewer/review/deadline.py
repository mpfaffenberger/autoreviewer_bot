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

FIVE_MINUTES = (
    "Five minutes or less remain before review wrap-up. Prioritize unresolved high-impact evidence; "
    "finish your report as soon as coverage is complete. Do not start speculative investigations."
)
ONE_MINUTE = (
    "One minute or less remains before review wrap-up. Finish immediately: return the final structured "
    "result using evidence already gathered. Do not claim coverage you did not complete."
)

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
    def remaining(self) -> float:
        return self.soft_s - (time.monotonic() - self.started)

    @property
    def passed(self) -> bool:
        return self.remaining <= 0

    def watch(self, label: str) -> AbstractCapability[None]:
        """A capability that makes the lens labelled `label` wrap up once the deadline has passed."""
        return _WrapUp(self, label)


@dataclass
class _WrapUp(AbstractCapability[None]):
    deadline: Deadline
    label: str
    last_warning: str | None = None

    async def prepare_tools(self, ctx: RunContext[None], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
        # Output tools are separate: the final report stays available.
        return [] if self.deadline.remaining <= 60 else tool_defs

    async def before_model_request(
        self, ctx: RunContext[None], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        remaining = self.deadline.remaining
        if remaining > 300:
            return request_context
        warning = WRAP_UP if remaining <= 0 else ONE_MINUTE if remaining <= 60 else FIVE_MINUTES
        last = request_context.messages[-1]
        if isinstance(last, ModelRequest) and warning != self.last_warning:
            last.parts = [*last.parts, UserPromptPart(warning)]
            self.last_warning = warning
            if remaining <= 60:
                self.deadline.wrapped_up.add(self.label)
        return request_context
