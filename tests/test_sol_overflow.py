"""Exercise receiver wiring without sending requests to either model provider."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from autoreviewer.review.agents import lens_version
from autoreviewer.review.job import ReviewDeps
from autoreviewer.review.memory import ReviewMemory
from autoreviewer.review.workspace import Workspace
from autoreviewer.web.app import SOL, ModelPlan, sol_overflow_deps
from autoreviewer.web.config import AGENT_KINDS, Settings, sol_overflow_repos_from
from tests.test_prism_model_plan import APP_ENV


def test_allowlist_from_env() -> None:
    env = {**APP_ENV, "BOODLETON_API_KEY": "fake"}
    assert sol_overflow_repos_from({"PRISM_SOL_OVERFLOW_REPOS": " * "}) == {"*"}
    assert not Settings.from_env(env).sol_overflow_repos
    settings = Settings.from_env({**env, "PRISM_SOL_OVERFLOW_REPOS": " Org/A, org/a, Other-Owner/.github "})
    assert settings.sol_overflow_repos == {"org/a", "other-owner/.github"}


@pytest.mark.parametrize(
    "bad", ["*,org/repo", "org/*", "org/re?o", "org", "org/", "/repo", "a/b/c", "org/..", "a/b,,c/d"]
)
def test_reject_invalid_allowlist(bad: str) -> None:
    with pytest.raises(ValueError, match="PRISM_SOL_OVERFLOW_REPOS"):
        sol_overflow_repos_from({"PRISM_SOL_OVERFLOW_REPOS": bad})


@pytest.mark.parametrize("portable", [False, True])
def test_sol_overrides_only_finders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, portable: bool) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))  # Disabled must not even need credentials.
    path = tmp_path / "prism.json"
    path.write_text('{"finder":"openai:test-finder","checker":"openai:test-checker"}')
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    env = {**APP_ENV, "BOODLETON_API_KEY": "fake", "PRISM_CONFIG": str(path) if portable else ""}
    settings = replace(Settings.from_env(env), docs=True, tests=True)
    plan = ModelPlan.from_settings(settings)
    normal = ReviewDeps(Mock(), Mock(), Mock(), tmp_path, "bot", "normal", memory=ReviewMemory(tmp_path))
    assert sol_overflow_deps(settings, normal, plan) is None
    settings = replace(settings, sol_overflow_repos=frozenset({"org/repo"}))
    (tmp_path / "auth.json").write_text('{"tokens":{"account_id":"fake","access_token":"fake","refresh_token":"fake"}}')
    overflow = sol_overflow_deps(settings, normal, plan)
    assert overflow and overflow.memory is None and normal.memory is not None
    assert overflow.source is normal.source and overflow.github is normal.github
    assert overflow.lens_version == lens_version(SOL) and SOL in overflow.models_note
    workspace = Workspace(tmp_path, "base", "head")
    agents = {spec.kind: spec.agents(workspace) for spec in overflow.lenses}
    assert overflow.simplifier
    simplify = overflow.simplifier(workspace)
    pairs = {**agents, "correctness": overflow.agents(workspace), "simplify": (simplify.propose, simplify.check)}
    assert pairs.keys() == set(AGENT_KINDS)
    for kind in AGENT_KINDS:
        finder, checker = pairs[kind]
        assert finder.model and f"{finder.model.system}:{finder.model.model_name}" == SOL
        assert checker.model is plan.verifier(kind)[0] and checker.model is not finder.model
    assert plan.finder("correctness")[0] is not pairs["correctness"][0].model


@pytest.mark.parametrize("checker", [SOL, "openai:gpt-6.2-sol"])
def test_sol_cannot_check_its_own_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checker: str) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    path = tmp_path / "prism.json"
    path.write_text(f'{{"finder":"openai:finder","checker":"{checker}"}}')
    settings = Settings.from_env({**APP_ENV, "PRISM_CONFIG": str(path), "PRISM_SOL_OVERFLOW_REPOS": "org/repo"})
    with pytest.raises(ValueError, match="checker"):
        sol_overflow_deps(settings, Mock(spec=ReviewDeps), ModelPlan.from_settings(settings))
