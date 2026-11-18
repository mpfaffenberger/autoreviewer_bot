"""Per-model lanes: every agent on a model shares one limit, so the bot never floods a model server."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoreviewer.review.agents import with_lanes
from autoreviewer.web.config import DEFAULT_MODEL_LANES, parse_lanes

pytestmark = pytest.mark.anyio


async def test_agents_on_one_model_share_its_lanes() -> None:
    in_flight, peak = [0], [0]

    async def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        in_flight[0] += 1
        peak[0] = max(peak[0], in_flight[0])
        await asyncio.sleep(0.05)
        in_flight[0] -= 1
        return ModelResponse(parts=[TextPart("ok")])

    # Separate agents and separate model objects, as the lenses and verifiers of a review are.
    agents = [Agent(with_lanes(FunctionModel(slow), "shared-test-model", 2)) for _ in range(6)]
    await asyncio.gather(*(agent.run("go") for agent in agents))
    assert peak[0] == 2


def test_lanes_parse_strictly() -> None:
    assert parse_lanes(" qwen=3 , deepseek-v4.1-flash=4 ,") == {"qwen": 3, "deepseek-v4.1-flash": 4}
    for bad in ("qwen", "qwen=0", "=3", "qwen=three"):
        with pytest.raises(ValueError, match="model=lanes"):
            parse_lanes(bad)


def test_the_defaults_match_the_model_servers() -> None:
    assert DEFAULT_MODEL_LANES == {"qwen3.8-flash-next": 3, "deepseek-v4.1-flash": 4}
