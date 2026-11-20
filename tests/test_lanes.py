"""Per-model lanes: every agent on a model shares one limit, so the bot never floods a model server."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoreviewer.review.agents import _lanes, boodleton_model, with_lanes  # pyright: ignore[reportPrivateUsage]
from autoreviewer.servers import COMPACT_AT, CONTEXT_WINDOWS, DEEPSEEK, QWEN, REPLY_TOKENS, ReasoningEffort
from autoreviewer.web.app import ModelPlan
from autoreviewer.web.config import DEFAULT_MODEL_LANES, Settings, parse_lanes

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
    assert DEFAULT_MODEL_LANES == {QWEN: 5, DEEPSEEK: 4}


@pytest.mark.parametrize("value", [None, "", "  "])
def test_a_missing_or_blank_setting_keeps_the_default_lanes(value: str | None) -> None:
    env = {"GITHUB_APP_ID": "1", "GITHUB_APP_SLUG": "s", "GITHUB_WEBHOOK_SECRET": "w", "BOODLETON_API_KEY": "b",
           "AUTOREVIEWER_PRIVATE_KEY_PATH": __file__}  # fmt: skip
    if value is not None:
        env["AUTOREVIEWER_MODEL_LANES"] = value
    assert Settings.from_env(env).model_lanes == DEFAULT_MODEL_LANES


def test_every_planned_agent_waits_in_its_models_lanes() -> None:
    """From the bot's review of #41: the plan the bot builds attaches each model's process-wide limiter."""
    settings = Settings(app_id=1, app_slug="s", webhook_secret="w", private_key="k", boodleton_api_key="b")
    plan = ModelPlan.from_settings(settings)  # the bot's own path, from its settings
    for kind in ("correctness", "docs"):
        for role, (model, _) in ((kind, plan.finder(kind)), (f"{kind}.verifier", plan.verifier(kind))):
            name = plan.models[role]
            assert model._limiter is _lanes(name, DEFAULT_MODEL_LANES[name]), role  # type: ignore[attr-defined]


def test_every_prompt_leaves_room_for_the_longest_reply() -> None:
    """Qwen's server refuses a prompt plus max_tokens over its window (Nov 19: a 262,755-token prompt lost #41's
    review). Compacting at COMPACT_AT and asking for REPLY_TOKENS always fits, for every model."""
    for model, window in CONTEXT_WINDOWS.items():
        assert COMPACT_AT[model] + REPLY_TOKENS <= window, model


@pytest.mark.parametrize("reasoning", [None, "high"])
def test_every_request_says_how_long_its_reply_may_be(reasoning: ReasoningEffort | None) -> None:
    """Otherwise Qwen's server reserves its default, 32,768, and refuses prompts past window minus that."""
    model = boodleton_model(QWEN, base_url="http://boodleton.invalid/v1", api_key="k", reasoning=reasoning)
    assert model.settings == {
        "max_tokens": REPLY_TOKENS,
        **({"openai_reasoning_effort": reasoning} if reasoning else {}),
    }
