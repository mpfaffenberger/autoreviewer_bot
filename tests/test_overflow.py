"""Exercise receiver wiring without sending requests to either model provider."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel

from autoreviewer.review.agents import lens_version
from autoreviewer.review.job import ReviewDeps
from autoreviewer.review.memory import ReviewMemory
from autoreviewer.review.workspace import Workspace
from autoreviewer.servers import DEFAULT_OVERFLOW_MODEL
from autoreviewer.web.app import ModelPlan, overflow_deps
from autoreviewer.web.config import AGENT_KINDS, LEGACY_OVERFLOW_REPOS, OVERFLOW_REPOS, Settings, overflow_repos_from
from tests.test_prism_model_plan import APP_ENV


@pytest.mark.parametrize("key", [OVERFLOW_REPOS, LEGACY_OVERFLOW_REPOS])
def test_allowlist_from_env(key: str) -> None:
    """The setting's former name still works, so a deployed unit survives the upgrade."""
    env = {**APP_ENV, "BOODLETON_API_KEY": "fake"}
    assert overflow_repos_from({key: " * "}) == {"*"}
    assert not Settings.from_env(env).overflow_repos
    settings = Settings.from_env({**env, key: " Org/A, org/a, Other-Owner/.github "})
    assert settings.overflow_repos == {"org/a", "other-owner/.github"}


def test_both_allowlist_names_must_agree() -> None:
    assert overflow_repos_from({OVERFLOW_REPOS: "a/b", LEGACY_OVERFLOW_REPOS: "a/b"}) == {"a/b"}
    with pytest.raises(ValueError, match="disagree"):
        overflow_repos_from({OVERFLOW_REPOS: "a/b", LEGACY_OVERFLOW_REPOS: "*"})


@pytest.mark.parametrize(
    "bad", ["*,org/repo", "org/*", "org/re?o", "org", "org/", "/repo", "a/b/c", "org/..", "a/b,,c/d"]
)
def test_reject_invalid_allowlist(bad: str) -> None:
    with pytest.raises(ValueError, match=f"^{OVERFLOW_REPOS}:"):
        overflow_repos_from({OVERFLOW_REPOS: bad})


@pytest.mark.parametrize("portable", [False, True])
def test_overrides_only_finders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, portable: bool) -> None:
    path = tmp_path / "prism.json"
    path.write_text(
        json.dumps(
            {
                "finder": "openai:test-finder",
                "checker": "openai:test-checker",
                "overflow": "local:burst",
                "endpoints": {"local:burst": "http://localhost:8001/v1"},
            }
        )
    )
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    env = {**APP_ENV, "BOODLETON_API_KEY": "fake", "PRISM_CONFIG": str(path) if portable else ""}
    settings = replace(Settings.from_env(env), docs=True, tests=True)
    # The overflow model comes from whichever configuration is active, never from code.
    assert settings.overflow_model == ("local:burst" if portable else DEFAULT_OVERFLOW_MODEL)
    spec = "local:burst" if portable else "openai:deployment-default"  # a profile value needing no credentials
    settings = replace(settings, overflow_model=spec)
    plan = ModelPlan.from_settings(settings)
    normal = ReviewDeps(Mock(), Mock(), Mock(), tmp_path, "bot", "normal", memory=ReviewMemory(tmp_path))
    assert overflow_deps(settings, normal, plan) is None
    settings = replace(settings, overflow_repos=frozenset({"org/repo"}))
    overflow = overflow_deps(settings, normal, plan)
    assert overflow and overflow.memory is None and normal.memory is not None
    assert overflow.source is normal.source and overflow.github is normal.github
    assert overflow.lens_version == lens_version(spec) and spec in overflow.models_note
    workspace = Workspace(tmp_path, "base", "head")
    agents = {spec.kind: spec.agents(workspace) for spec in overflow.lenses}
    assert overflow.simplifier
    simplify = overflow.simplifier(workspace)
    pairs = {**agents, "correctness": overflow.agents(workspace), "simplify": (simplify.propose, simplify.check)}
    assert pairs.keys() == set(AGENT_KINDS)
    for kind in AGENT_KINDS:
        finder, checker = pairs[kind]
        assert finder.model and finder.model.model_name == spec.split(":", 1)[1]
        assert not isinstance(finder.model, ConcurrencyLimitedModel)
        assert finder.model is pairs["correctness"][0].model
        assert checker.model is plan.verifier(kind)[0] and checker.model is not finder.model
    assert plan.finder("correctness")[0] is not pairs["correctness"][0].model


@pytest.mark.parametrize("spec", ["openai:checker", "local:checker", None])
def test_no_implicit_or_nonindependent_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spec: str | None
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    path = tmp_path / "prism.json"
    path.write_text(
        json.dumps(
            {
                "finder": "openai:finder",
                "checker": "openai:checker",
                "overflow": spec,
                "endpoints": {spec: "http://localhost:8000/v1"} if spec == "local:checker" else {},
            }
        )
    )
    with pytest.raises(ValueError, match="checker" if spec else "overflow model"):
        # With `overflow` unset, a portable config must not borrow the deployment profile's model.
        settings = Settings.from_env({**APP_ENV, "PRISM_CONFIG": str(path), OVERFLOW_REPOS: "org/repo"})
        overflow_deps(settings, Mock(spec=ReviewDeps), ModelPlan.from_settings(settings))
