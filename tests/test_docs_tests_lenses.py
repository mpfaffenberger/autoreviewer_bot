"""The docs and tests lenses: advisory, capped, off until their evals are in, found by DeepSeek, checked by Kimi."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoreviewer.review.agents import LENS_PROMPTS, PROMPTS, lens_pair
from autoreviewer.review.pipeline import blocks
from autoreviewer.review.schemas import Finding
from autoreviewer.review.workspace import Workspace
from autoreviewer.servers import DEEPSEEK, KIMI
from autoreviewer.web.app import MAX_ADVISORY_POSTED, build_lenses, models_note
from autoreviewer.web.config import Settings

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
        assert (finder.model.model_name, checker.model.model_name) == (DEEPSEEK, KIMI)  # type: ignore[union-attr]
        assert (spec.max_posted, spec.whole_pr) == (MAX_ADVISORY_POSTED, False)


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
