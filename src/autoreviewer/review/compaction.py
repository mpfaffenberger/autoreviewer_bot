"""Context compaction for the review agents, built from pydantic-ai-harness strategies.

A reviewer explores the change with tools, so its history is mostly tool results that go stale once
read. One `TieredCompaction` runs before every model request and, only when the conversation is over
its target, escalates cheapest-first until it fits again:

1. clamp any single oversized part (a runaway generation would overflow on its own);
2. drop earlier copies of an identical tool call (the checkout is read-only, so the output is too);
3. blank old tool results, keeping the latest pairs (the reviewer can re-run a tool);
4. summarize older history with the run's own model (the only tier that costs a request).

Those tiers never shrink the newest tool results, so each one is bounded where it's produced instead
(`tool_output_limits`): a result over its share of the window is spilled, and the reviewer pages through
it with `read_tool_result`. Without that bound, a few big reads overflowed Qwen's window (Nov 19: a
262,755-token prompt against 229,376, which cost #41 its review).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import timedelta

from pydantic_ai.messages import ModelMessage, ModelRequest, ToolCallPart, ToolReturnPart
from pydantic_ai_harness.compaction import (
    ClampOversizedMessages,
    ClearToolResults,
    DeduplicateFileReads,
    SummarizingCompaction,
    TieredCompaction,
)
from pydantic_ai_harness.tool_output_limits import (
    READ_TOOL_NAME,
    Band,
    LocalFileStore,
    Spill,
    ToolOutputLimits,
    Truncate,
)

TARGET_FRACTION = 0.6
"""Compact above 60% of the window: room for the next tool result and the model's reply."""

KEEP_RECENT_TOOL_PAIRS = 6

CLEARED = "[tool result cleared: call the tool again if you still need it]"
SUPERSEDED = "[same call made again later: see the newer result]"

IDEMPOTENT_TOOLS = frozenset(
    {
        "changed_files",
        "diff",
        "show_file",
        "read_file",
        "grep",
        "list_files",
        "search_files",
        "find_files",
        "file_info",
        READ_TOOL_NAME,
    }
)
"""Read-only tools over an immutable checkout, and the spill reader over a spill, which never changes either:
the same call returns the same output. Once a spill is pruned its reader returns nothing, and the reviewer
calls the original tool again."""


def same_call(call: ToolCallPart) -> str | None:
    """Identity for deduplication: tool name plus its exact arguments, for read-only tools only."""
    if call.tool_name not in IDEMPOTENT_TOOLS:
        return None
    return f"{call.tool_name}:{json.dumps(call.args_as_dict(), sort_keys=True, default=str)}"


def review_compaction(context_window: int) -> TieredCompaction[None]:
    return TieredCompaction(
        tiers=[
            ClampOversizedMessages(max_part_tokens=context_window // 4),
            # Inside TieredCompaction each tier's own trigger is bypassed; max_tokens=1 just satisfies
            # the "set a trigger" requirement, as in the harness's own example.
            DeduplicateFileReads(file_key=same_call, placeholder=SUPERSEDED, max_tokens=1),
            ClearToolResults(max_tokens=1, keep_pairs=KEEP_RECENT_TOOL_PAIRS, placeholder=CLEARED),
            SummarizingCompaction(max_tokens=1),  # model=None: summarize with the run's own model
        ],
        target_fraction=TARGET_FRACTION,
        context_window=context_window,
    )


def max_result_tokens(context_window: int) -> int:
    """The most one tool result may cost: the results ClearToolResults keeps, plus a couple more arriving in
    the same step, fill at most the compaction target."""
    return int(context_window * TARGET_FRACTION) // (KEEP_RECENT_TOOL_PAIRS + 2)


SPILLS = LocalFileStore(cleanup_after=timedelta(days=7))
"""Spilled results, in the harness's per-user temp directory: the checkout is read-only. A cache, not state: a
continued conversation may read an old handle again, and one that's gone (a week on, or after a restart, since
the container's /tmp isn't kept) just means calling the tool again."""


def tool_output_limits(context_window: int) -> ToolOutputLimits[None]:
    """Bound every tool result when it's produced: over `max_result_tokens`, it's spilled, and the reviewer gets
    a preview and a handle to page through the rest (lossless). If the spill can't be written, it's truncated."""
    return ToolOutputLimits(
        bands=[Band(over=max_result_tokens(context_window), action=Spill(then=Truncate()))],
        over_tokens=True,
        store=SPILLS,
    )


def compaction_stats(messages: Sequence[ModelMessage]) -> dict[str, int]:
    """How much of a finished run's history was compacted away (for the review log)."""
    returns = [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    return {
        "cleared": sum(1 for part in returns if part.content == CLEARED),
        "superseded": sum(1 for part in returns if part.content == SUPERSEDED),
    }
