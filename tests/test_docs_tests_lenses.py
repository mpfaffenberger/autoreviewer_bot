"""The docs and tests lenses: advisory, capped, off until their evals are in, found by DeepSeek, checked by Kimi."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoreviewer.review.agents import LENS_PROMPTS, PROMPTS, lens_pair
from autoreviewer.review.pipeline import blocks
from autoreviewer.review.schemas import Finding
from autoreviewer.review.workspace import Workspace
from autoreviewer.servers import CONTEXT_WINDOWS, DEEPSEEK, KIMI, QWEN
from autoreviewer.web.app import MAX_ADVISORY_POSTED, ModelPlan, build_lenses, models_note
from autoreviewer.web.config import Settings, models_from

BASE = Settings(app_id=1, app_slug="s", webhook_secret="w", private_key="k", boodleton_api_key="b")
ENV = {"GITHUB_APP_ID": "1", "GITHUB_APP_SLUG": "s", "GITHUB_WEBHOOK_SECRET": "w", "BOODLETON_API_KEY": "b",
       "AUTOREVIEWER_PRIVATE_KEY_PATH": __file__}  # fmt: skip


def with_lenses(**switches: bool) -> Settings:
    return Settings(**{**BASE.__dict__, **switches})  # type: ignore[arg-type]


def test_off_by_default_and_switched_on_by_env() -> None:
    assert (Settings.from_env(ENV).docs, Settings.from_env(ENV).tests) == (False, False)
    on = Settings.from_env({**ENV, "AUTOREVIEWER_DOCS": "true", "AUTOREVIEWER_TESTS": "true"})
    assert (on.docs, on.tests) == (True, True)


@pytest.mark.anyio
async def test_found_by_deepseek_checked_by_kimi_capped_and_scoped(workspace: Workspace) -> None:
    lenses = {spec.kind: spec for spec in build_lenses(with_lenses(docs=True, tests=True))}
    for kind in ("docs", "tests"):
        spec = lenses[kind]
        finder, checker = spec.agents(workspace)
        assert finder.model.model_name == DEEPSEEK  # type: ignore[union-attr]
        assert checks_in_order(checker.model) == [KIMI, QWEN]  # Kimi, and Qwen when Kimi fails
        assert (spec.max_posted, spec.whole_pr) == (MAX_ADVISORY_POSTED, False)


def checks_in_order(model: object) -> list[str]:
    """The models a request tries, in order: one, or a FallbackModel's chain."""
    chain = model.models if isinstance(model, FallbackModel) else [model]
    return [m.model_name for m in chain]  # type: ignore[union-attr]


@pytest.mark.parametrize("kind", ["docs", "tests"])
def test_never_blocks(kind: str) -> None:
    finding = Finding(path="a.py", line=1, priority=0, confidence=10, title="t", explanation="e", kind=kind)  # type: ignore[arg-type]
    assert not blocks(finding, 3)


def test_the_footer_names_them() -> None:
    note = models_note(with_lenses(docs=True, tests=True, dry=False))
    assert note.endswith(f"; docs and tests by `{DEEPSEEK}`, verified by `{KIMI}`")


def test_every_lens_has_both_prompts() -> None:
    for kind, prompts in LENS_PROMPTS.items():
        for name in prompts:
            assert (PROMPTS / name).read_text().strip(), (kind, name)


class _Seen(Exception):
    """Stops a run once the model has been called: only the instructions it was sent matter here."""


@pytest.mark.anyio
async def test_the_docs_lens_and_its_checker_hold_text_to_unslop(workspace: Workspace) -> None:
    sent: dict[str, str] = {}

    def recording(role: str) -> FunctionModel:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            sent[role] = [m for m in messages if isinstance(m, ModelRequest)][-1].instructions or ""
            raise _Seen

        return FunctionModel(respond)

    for kind in ("docs", "tests", "dry"):
        for agent in lens_pair(kind, recording(kind), recording(f"{kind}.verifier"), workspace):
            with pytest.raises(_Seen):
                await agent.run("go")
    rule = '23. Cut filler. "In order to" becomes "to".'
    assert [role for role, instructions in sent.items() if rule in instructions] == ["docs", "docs.verifier"]


def test_lenses_run_in_one_order() -> None:
    every = with_lenses(security=True, dry=True, docs=True, tests=True)
    assert [spec.kind for spec in build_lenses(every)] == ["security", "dry", "docs", "tests"]


def test_only_models_with_a_fallback_get_one() -> None:
    """Kimi, a hosted API with quotas, falls back to Qwen; the window fits both. DeepSeek runs alone."""
    plan = ModelPlan.from_settings(BASE)
    model, window = plan.verifier("docs")
    assert (checks_in_order(model), window) == ([KIMI, QWEN], min(CONTEXT_WINDOWS[KIMI], CONTEXT_WINDOWS[QWEN]))
    assert checks_in_order(plan.verifier("correctness")[0]) == [DEEPSEEK]


def test_a_checker_may_not_fall_back_to_its_finders_model() -> None:
    with pytest.raises(ValueError, match=r"docs\.verifier \(kimi-k3\.1-pro\) falls back to 'qwen3\.8-flash-next'"):
        models_from({"AUTOREVIEWER_MODELS": f"docs={QWEN}"})
