"""Reasoning effort per role: DeepSeek doesn't think unless a request asks it to."""

from __future__ import annotations

import pytest
from pydantic_ai.models import Model

from autoreviewer.review import agents
from autoreviewer.review.agents import lens_version
from autoreviewer.servers import QWEN
from autoreviewer.web.app import ModelPlan
from autoreviewer.web.config import Settings, models_from, reasoning_from


def test_efforts_parse_strictly() -> None:
    assert reasoning_from({}) == {"conversation": "high"}  # it decides withdrawals
    pinned = reasoning_from({"AUTOREVIEWER_REASONING": " docs=high, tests=low ,conversation=low"})
    assert pinned == {"docs": "high", "tests": "low", "conversation": "low"}
    for value, error in (("doc=high", "unknown role 'doc'"), ("docs=max", "docs='max'"), ("docs", "role=effort")):
        with pytest.raises(ValueError, match=error):
            reasoning_from({"AUTOREVIEWER_REASONING": value})


def effort(model: Model) -> object:
    """The reasoning effort every request from `model` carries (through the lanes wrapper)."""
    return (getattr(model, "wrapped", model).settings or {}).get("openai_reasoning_effort")


def test_only_the_listed_roles_ask_for_reasoning() -> None:
    plan = ModelPlan(models_from({}), "http://boodleton.invalid/v1", "k", {}, reasoning_from(
        {"AUTOREVIEWER_REASONING": "docs=high,security.verifier=medium"}
    ))  # fmt: skip
    assert effort(plan.finder("docs")[0]) == "high"
    assert effort(plan.verifier("docs")[0]) is None  # Qwen checks it, thinking by default
    assert effort(plan.verifier("security")[0]) == "medium"
    assert effort(plan.finder("tests")[0]) is None  # the same model (DeepSeek), but no effort listed for it


def test_the_bots_settings_carry_the_efforts_to_its_agents() -> None:
    """From the bot's review of #36: the path the bot takes, from its environment to the requests."""
    env = {"GITHUB_APP_ID": "1", "GITHUB_APP_SLUG": "s", "GITHUB_WEBHOOK_SECRET": "w", "BOODLETON_API_KEY": "b",
           "AUTOREVIEWER_PRIVATE_KEY_PATH": __file__, "AUTOREVIEWER_REASONING": "docs=high"}  # fmt: skip
    plan = ModelPlan.from_settings(Settings.from_env(env))
    assert (effort(plan.finder("docs")[0]), effort(plan.finder("tests")[0])) == ("high", None)


def test_a_new_effort_starts_the_lens_conversation_afresh() -> None:
    """A saved correctness conversation is continued only with the same model and the same reasoning."""
    assert lens_version(QWEN) != lens_version(QWEN, "high") != lens_version(QWEN, "low")


def test_the_conversation_agent_thinks_by_default() -> None:
    """It decides withdrawals: DeepSeek doesn't think unless asked."""
    settings = Settings(app_id=1, app_slug="s", webhook_secret="w", private_key="k", boodleton_api_key="b")
    assert effort(ModelPlan.from_settings(settings).conversation()[0]) == "high"


def test_the_lens_tools_are_part_of_its_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """From the bot's review of #43: read_tool_result joining the tools started saved conversations afresh."""
    before = lens_version(QWEN)
    monkeypatch.setattr(agents, "READ_TOOL_NAME", "read_spill")
    assert lens_version(QWEN) != before
