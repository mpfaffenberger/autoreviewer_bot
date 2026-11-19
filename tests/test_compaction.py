"""Compaction in real agent runs: the lens agent, its git tools, and the harness tiers, with a scripted model."""

from __future__ import annotations

import re
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_harness.tool_output_limits import READ_TOOL_NAME

from autoreviewer.review.agents import LENS_LIMITS, correctness_agent
from autoreviewer.review.compaction import compaction_stats, max_result_tokens, same_call
from autoreviewer.review.workspace import Workspace

pytestmark = pytest.mark.anyio


def scripted(calls: list[tuple[str, dict[str, Any]]], summaries: list[str]) -> FunctionModel:
    """Make each tool call in turn, then return no findings. Summarization requests get a summary."""
    remaining = iter(calls)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not info.function_tools and not info.output_tools:  # the summarizer's own request
            summaries.append("called")
            return ModelResponse(parts=[TextPart("Summary: reviewed stats.py; no bugs confirmed yet.")])
        if (call := next(remaining, None)) is not None:
            return ModelResponse(parts=[ToolCallPart(tool_name=call[0], args=call[1])])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=info.output_tools[0].name, args={"findings": [], "overall_correct": True, "reviewed": []}
                )
            ]
        )

    return FunctionModel(respond)


async def run(workspace: Workspace, calls: list[tuple[str, dict[str, Any]]], window: int) -> tuple[dict[str, int], int]:
    summaries: list[str] = []
    agent = correctness_agent(scripted(calls, summaries), workspace, context_window=window)
    result = await agent.run("Review the change.", usage_limits=LENS_LIMITS)
    assert result.output.findings == []
    return compaction_stats(result.all_messages()), len(summaries)


def test_same_call_keys_exact_read_only_calls() -> None:
    def call(name: str, **args: object) -> ToolCallPart:
        return ToolCallPart(tool_name=name, args=args)

    assert same_call(call("diff", path="a.py")) == same_call(call("diff", path="a.py"))
    assert same_call(call("diff", path="a.py")) != same_call(call("diff", path="b.py"))
    assert same_call(call("diff", path="a.py", offset=0)) != same_call(call("diff", path="a.py", offset=400))
    assert same_call(call("final_result", findings=[])) is None


async def test_repeated_identical_calls_are_deduplicated(workspace: Workspace) -> None:
    # Windows here are tiny because FunctionModel reports roughly word counts as tokens; the measured
    # thresholds under it: dedupe alone at 2,400, all tiers by 1,400. Real models report real tokens.
    stats, summaries = await run(workspace, [("diff", {})] * 12, window=2_400)
    assert stats["superseded"] > 0 and summaries == 0


async def test_old_distinct_results_are_cleared(workspace: Workspace) -> None:
    calls: list[tuple[str, dict[str, Any]]] = [("diff", {"limit": 100 + i}) for i in range(12)]  # all distinct
    stats, _ = await run(workspace, calls, window=1_800)
    assert stats["cleared"] > 0 and stats["superseded"] == 0


async def test_when_cheap_passes_are_not_enough_the_history_is_summarized(workspace: Workspace) -> None:
    calls: list[tuple[str, dict[str, Any]]] = [("diff", {"limit": 100 + i}) for i in range(14)]
    _, summaries = await run(workspace, calls, window=1_200)
    assert summaries > 0  # and the run still finished with its structured output (asserted in run())


async def test_the_review_still_works_without_any_compaction(workspace: Workspace) -> None:
    stats, summaries = await run(workspace, [("diff", {}), ("changed_files", {})], window=1_000_000)
    assert stats == {"cleared": 0, "superseded": 0} and summaries == 0


@pytest.mark.parametrize(("window", "spilled"), [(40_000, True), (400_000, False)])
async def test_a_big_tool_result_is_spilled_and_read_back_in_full(
    workspace: Workspace, window: int, spilled: bool
) -> None:
    """Nov 19: nothing bounded a tool result, and a 262,755-token prompt cost #41 its review. A result over its
    share of the window is spilled: the history keeps a preview and a handle, and nothing is lost."""
    (workspace.root / "big.py").write_text("".join(f"# line {i}: abc abc abc abc abc abc abc\n" for i in range(4000)))
    returns: dict[str, str] = {}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns.update(
            {p.tool_name: str(p.content) for m in messages for p in m.parts if isinstance(p, ToolReturnPart)}
        )
        if "read_file" not in returns:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "big.py"})])
        if spilled and READ_TOOL_NAME not in returns:
            handle = re.search(r"handle='([^']+)'", returns["read_file"])
            assert handle, returns["read_file"][:300]
            return ModelResponse(parts=[ToolCallPart(READ_TOOL_NAME, {"handle": handle[1], "pattern": "line 700:"})])
        done = {"findings": [], "overall_correct": True, "reviewed": []}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, done)])

    agent = correctness_agent(FunctionModel(respond), workspace, context_window=window)
    await agent.run("Review the change.", usage_limits=LENS_LIMITS)
    if not spilled:  # ~12k tokens fit a 400k window's ~30k budget: passed through whole
        assert "# line 700: abc" in returns["read_file"] and READ_TOOL_NAME not in returns
        return
    assert len(returns["read_file"]) < max_result_tokens(window)  # in characters, so certainly in tokens
    assert "# line 700: abc" not in returns["read_file"]  # past the preview...
    assert "# line 700: abc" in returns[READ_TOOL_NAME]  # ...and still readable
