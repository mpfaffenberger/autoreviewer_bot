"""Time limits for a review.

On its next step, a lens with five minutes left is asked to focus. With one minute left it loses
its function tools and must return its final report. Both windows shrink proportionally for soft
budgets under the default (`DEFAULT_SOFT_S`). After soft expiry the instruction says time is up.

The hard deadline (`asyncio.timeout` in the job) cancels work still running. A review cut short
by either limit may request changes for what it found, but incomplete blocking work never approves.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import ToolDefinition

MAX_REVIEW_S = 600.0
"""No review attempt runs longer than this, whatever a deployment configures: the one definition of the cap."""
DEFAULT_SOFT_S = 540.0
"""The default soft budget, which leaves a minute to verify and post. The countdown below is sized for it."""
FOCUS_S = 300
REPORT_ONLY_S = 60

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
    """Labels of lenses forced to report, whether by instruction or by removal of function tools."""

    @property
    def remaining(self) -> float:
        return self.soft_s - (time.monotonic() - self.started)

    def watch(self, label: str) -> AbstractCapability[None]:
        """Warn the labelled lens as time runs short and force it to report before soft expiry."""
        return _WrapUp(self, label)


@dataclass
class _WrapUp(AbstractCapability[None]):
    deadline: Deadline
    label: str
    last_warning: str | None = None

    def _notice(self) -> str | None:
        remaining = self.deadline.remaining
        scale = min(1.0, max(0.0, self.deadline.soft_s) / DEFAULT_SOFT_S)
        if remaining <= REPORT_ONLY_S * scale:
            self.deadline.wrapped_up.add(self.label)
            return WRAP_UP if remaining <= 0 else ONE_MINUTE
        if remaining <= FOCUS_S * scale:
            return FIVE_MINUTES
        return None

    async def prepare_tools(self, ctx: RunContext[None], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
        # Returning [] removes only function tools. The separate output tools still allow a final report.
        return [] if self._notice() in (ONE_MINUTE, WRAP_UP) else tool_defs

    async def before_model_request(
        self, ctx: RunContext[None], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        warning = self._notice()
        if warning is None:
            return request_context
        last = request_context.messages[-1]
        if isinstance(last, ModelRequest) and warning != self.last_warning:
            last.parts = [*last.parts, UserPromptPart(warning)]
            self.last_warning = warning
        return request_context
