"""Stuck models are stopped: a verifier gets a step budget, and every agent a loop detector."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_harness import DetectLoops

from autoreviewer.review.agents import VERIFIER_LIMITS, correctness_agent, verifier_agent
from autoreviewer.review.pipeline import review
from autoreviewer.review.workspace import Workspace
from tests.review_fakes import finding, lens_model
from tests.test_review import pr_text

pytestmark = pytest.mark.anyio


async def test_a_verifier_rereading_a_file_forever_is_stopped(workspace: Workspace) -> None:
    """What a live security verifier did: read_file one line further each time, hundreds of times."""
    steps: list[int] = []

    def rereading(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        steps.append(len(steps))
        return ModelResponse(parts=[ToolCallPart("read_file", {"path": "stats.py", "offset": len(steps) % 5})])

    lens = correctness_agent(lens_model([finding(5)]), workspace)
    outcome = await review(await pr_text(workspace), lens, verifier_agent(FunctionModel(rereading), workspace))
    assert outcome.posted == [] and [reason for _, reason in outcome.unverified] == [
        "verification failed: UsageLimitExceeded"
    ]
    assert len(steps) == VERIFIER_LIMITS.request_limit


async def test_every_agent_watches_for_loops(workspace: Workspace) -> None:
    for agent in (correctness_agent(lens_model([]), workspace), verifier_agent(lens_model([]), workspace)):
        assert any(isinstance(capability, DetectLoops) for capability in agent.root_capability.capabilities)
