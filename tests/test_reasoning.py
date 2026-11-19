"""Reasoning effort per role: DeepSeek doesn't think unless a request asks it to."""

from __future__ import annotations

import pytest
from pydantic_ai.models import Model

from autoreviewer.web.app import ModelPlan
from autoreviewer.web.config import models_from, reasoning_from


def test_efforts_parse_strictly() -> None:
    assert reasoning_from({}) == {}
    assert reasoning_from({"AUTOREVIEWER_REASONING": " docs=high, tests=low ,"}) == {"docs": "high", "tests": "low"}
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
    assert effort(plan.verifier("docs")[0]) is None  # Kimi checks it, thinking by default
    assert effort(plan.verifier("security")[0]) == "medium"
    assert effort(plan.finder("tests")[0]) is None  # the same model (DeepSeek), but no effort listed for it
