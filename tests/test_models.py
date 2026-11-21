"""Pinning agents to models: one setting, checked at startup, used by every agent factory."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai_harness.compaction import TieredCompaction

from autoreviewer.review.workspace import Workspace
from autoreviewer.servers import COMPACT_AT
from autoreviewer.web.app import ModelPlan, build_agent_factory, build_lenses, build_simplifier_factory, models_note
from autoreviewer.web.config import AGENT_ROLES, Settings, models_from
from tests.reference_profile import DEEPSEEK, QWEN

PINS = f" security={DEEPSEEK}, security.verifier={QWEN} ,simplify.verifier={QWEN},simplify={DEEPSEEK}"
PINNED = {"PRISM_MODELS": PINS}


def settings(**changes: object) -> Settings:
    base = Settings(app_id=1, app_slug="s", webhook_secret="w", private_key="k", boodleton_api_key="b")
    return Settings(**{**base.__dict__, **changes})  # type: ignore[arg-type]


def test_default_models() -> None:
    """Finders on Qwen and checkers on DeepSeek, except docs and tests: found by DeepSeek, checked by Qwen."""
    expected = {role: DEEPSEEK if role.endswith(".verifier") else QWEN for role in AGENT_ROLES}
    expected |= {"docs": DEEPSEEK, "docs.verifier": QWEN, "tests": DEEPSEEK, "tests.verifier": QWEN}
    expected |= {"conversation": DEEPSEEK}
    assert models_from({}) == expected


def test_a_pin_moves_only_the_roles_it_names() -> None:
    models = models_from(PINNED)
    assert (models["security"], models["security.verifier"]) == (DEEPSEEK, QWEN)
    assert (models["correctness"], models["correctness.verifier"]) == (QWEN, DEEPSEEK)


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (f"securty={DEEPSEEK}", "unknown role 'securty'"),
        ("security=gpt-9", "no known context window for 'gpt-9'"),
        (f"dry={DEEPSEEK}", "dry and dry.verifier are both"),  # its checker is DeepSeek by default
        ("security", "expected role=model"),
    ],
)
def test_a_bad_pin_stops_startup(value: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        models_from({"PRISM_MODELS": value})


@pytest.mark.anyio
async def test_each_agent_gets_its_pinned_model_and_that_models_compaction_point(workspace: Workspace) -> None:
    pinned = settings(models=models_from(PINNED), dry=True)
    lenses = {spec.kind: spec.agents(workspace) for spec in build_lenses(pinned)}
    security, security_check = lenses["security"]
    assert (security.model.model_name, security_check.model.model_name) == (DEEPSEEK, QWEN)  # type: ignore[union-attr]
    dry, dry_check = lenses["dry"]
    assert (dry.model.model_name, dry_check.model.model_name) == (QWEN, DEEPSEEK)  # type: ignore[union-attr]
    lens, _ = build_agent_factory(pinned)(workspace)
    assert lens.model.model_name == QWEN  # type: ignore[union-attr]
    simplifier = build_simplifier_factory(pinned)
    assert simplifier is not None
    proposer = simplifier(workspace)
    assert (proposer.propose.model.model_name, proposer.check.model.model_name) == (DEEPSEEK, QWEN)  # type: ignore[union-attr]
    plan = ModelPlan.from_settings(pinned)
    assert (plan.finder("security")[1], plan.verifier("security")[1]) == (
        COMPACT_AT[DEEPSEEK],
        COMPACT_AT[QWEN],
    )


def test_the_footer_names_each_pair_of_models() -> None:
    note = models_note(settings(models=models_from(PINNED), dry=True))
    assert note == (
        f"correctness and DRY by `{QWEN}`, verified by `{DEEPSEEK}`; security by `{DEEPSEEK}`, verified by `{QWEN}`"
    )


def compacts_at(agent: Agent[None, Any]) -> int | None:
    """Where `agent` compacts its history: its TieredCompaction's target."""
    [compaction] = [c for c in agent.root_capability.capabilities if isinstance(c, TieredCompaction)]
    return compaction.target_tokens


@pytest.mark.anyio
async def test_each_built_agent_compacts_where_its_model_does(workspace: Workspace) -> None:
    """From the bot's review of #52: the agents the bot builds, not just what the plan returns. A Qwen agent
    compacting at DeepSeek's 629k would grow past its 168k window."""
    pinned = settings(models=models_from(PINNED), dry=True)
    lenses = {spec.kind: spec.agents(workspace) for spec in build_lenses(pinned)}
    simplifier = build_simplifier_factory(pinned)
    assert simplifier is not None
    proposer = simplifier(workspace)
    built = {
        "security": lenses["security"],
        "dry": lenses["dry"],
        "correctness": build_agent_factory(pinned)(workspace),
        "simplify": (proposer.propose, proposer.check),
    }
    for kind, (finder, checker) in built.items():
        expected = (COMPACT_AT[pinned.models[kind]], COMPACT_AT[pinned.models[f"{kind}.verifier"]])
        assert (compacts_at(finder), compacts_at(checker)) == expected, kind
