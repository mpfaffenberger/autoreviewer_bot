"""Portable config through the production agent factory, without network model requests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic_ai.models import Model
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.models.test import TestModel as LocalModel

from autoreviewer.model_config import PrismConfig
from autoreviewer.review.job import review_and_post
from autoreviewer.web.app import ModelPlan, build_agent_factory, models_note
from autoreviewer.web.config import AGENT_KINDS, Settings
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import FakeGitHub, GitRepo, finding, job_deps, lens_model, verifier_model

FINDER = f"factory:{__name__}:finder"
CHECKER = f"factory:{__name__}:checker"


def finder() -> Model:
    return lens_model([finding(5)])


def checker() -> Model:
    return verifier_model(lambda f: True)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    path = tmp_path / "prism.json"
    path.write_text(
        json.dumps(
            {"finder": FINDER, "checker": CHECKER, "lanes": {FINDER: 2}, "compact_at": {FINDER: 8192, CHECKER: 4096}}
        )
    )
    return Settings.from_env(
        {
            "GITHUB_APP_ID": "1",
            "GITHUB_APP_SLUG": "s",
            "GITHUB_WEBHOOK_SECRET": "w",
            "AUTOREVIEWER_PRIVATE_KEY_PATH": __file__,
            "PRISM_CONFIG": str(path),
        }
    )


def test_roles_and_lane_pools(settings: Settings) -> None:
    plan = ModelPlan.from_settings(settings)
    assert plan.lanes == {FINDER: 2, CHECKER: 1}
    shared, _ = plan.conversation()
    assert isinstance(shared, ConcurrencyLimitedModel)
    for kind in AGENT_KINDS:
        assert settings.models[kind] == FINDER and settings.models[f"{kind}.verifier"] == CHECKER
        assert plan.finder(kind)[1] == 8192
        model, target = plan.verifier(kind)
        assert isinstance(model, ConcurrencyLimitedModel) and model._limiter is shared._limiter
        assert target == 4096


@pytest.mark.anyio
async def test_portable_review(settings: Settings, repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo)
    deps = replace(job_deps(github, tmp_path), agents=build_agent_factory(settings), models_note=models_note(settings))
    pull = PullRequestUpdated(installation_id=1, owner="mpfaffenberger", repo="demo", number=7, head_sha=repo.head)
    result = await review_and_post(deps, pull)
    assert result.title == "1 blocking issue(s)"
    assert github.reviews[0]["event"] == "REQUEST_CHANGES"
    assert [(c.path, c.line) for c in github.inline_comments] == [("stats.py", 5)]


def test_installed_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    config = PrismConfig(finder="openai:gpt-4o", checker=CHECKER)
    model, target = config.resolve(config.finder)
    assert model.model_name == "gpt-4o" and model.system == "openai"
    window = model.profile.get("context_window")
    assert window and 0 < target < window


def small_window() -> Model:
    return LocalModel(profile={"context_window": 8192}, settings={"max_tokens": 6144})


def test_reply_reservation() -> None:
    spec = f"factory:{__name__}:small_window"
    assert PrismConfig(finder=spec, checker=CHECKER).resolve(spec)[1] == 2048
    with pytest.raises(ValueError, match="insufficient room"):
        PrismConfig(finder=spec, checker=CHECKER, compact_at={spec: 2049}).resolve(spec)


@pytest.mark.parametrize(
    "override",
    [
        {"finder": "bare"},
        {"finder": CHECKER},
        {"checker": "factory:pkg"},
        {"lanes": {FINDER: 0}},
        {"lanes": {FINDER: True}},
        {"compact_at": {"typo": 4096}},
        {"compact_at": {CHECKER: 7}},
    ],
)
def test_invalid_config(override: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        PrismConfig.model_validate({"finder": FINDER, "checker": CHECKER, **override})


def test_factory_must_return_model() -> None:
    config = PrismConfig(finder="factory:builtins:dict", checker=CHECKER)
    with pytest.raises(TypeError, match="must return"):
        config.resolve(config.finder)


@pytest.mark.parametrize("prefix", ["openai-codex", "github-copilot"])
def test_subscription_provider_resolves_offline(prefix: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": dict.fromkeys(["access_token", "refresh_token", "account_id"], "fake")})
    )
    monkeypatch.setenv("GITHUB_COPILOT_API_KEY", "fake")
    config = PrismConfig(finder=f"{prefix}:example-model", checker=CHECKER)
    model, _ = config.resolve(config.finder)
    assert model.system == prefix and model.model_name == "example-model"


def test_claude_code_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRISM_CLAUDE_CODE_API_KEY", raising=False)
    config = PrismConfig(finder="claude-code:my-model", checker=CHECKER)
    monkeypatch.setenv("PRISM_CLAUDE_CODE_BASE_URL", "https://gateway.example.org/v1")
    with pytest.raises(ValueError, match="PRISM_CLAUDE_CODE_API_KEY"):
        config.resolve(config.finder)
    monkeypatch.setenv("PRISM_CLAUDE_CODE_API_KEY", "fake")
    model, _ = config.resolve(config.finder)
    assert model.model_name == "my-model" and model.base_url == "https://gateway.example.org/v1/"


def test_native_claude_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = pytest.importorskip("pydantic_ai_claude_code")
    for key in ("PRISM_CLAUDE_CODE_BASE_URL", "PRISM_CLAUDE_CODE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_CREDENTIALS", "file")
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"access_token": "fake", "refresh_token": "fake"}))
    monkeypatch.setenv("CLAUDE_CODE_AUTH_FILE", str(auth))
    config = PrismConfig(finder="openai-codex:gpt-6.2-sol", checker="claude-code:claude-opus-5-5")
    model, _ = config.resolve(config.checker)
    assert isinstance(model, ConcurrencyLimitedModel) and isinstance(model.wrapped, adapter.ClaudeCodeModel)
    assert model.model_name == "claude-opus-5-5"
