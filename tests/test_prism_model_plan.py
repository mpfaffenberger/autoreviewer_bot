"""Regression tests for selecting Prism and sharing its models without sharing role settings."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic_ai.models.test import TestModel as LocalModel

from autoreviewer.github import GitHubApp
from autoreviewer.review.workspace import Workspace
from autoreviewer.web.app import ModelPlan, conversation_deps
from autoreviewer.web.config import Settings

APP_ENV = {
    "GITHUB_APP_ID": "1",
    "GITHUB_APP_SLUG": "s",
    "GITHUB_WEBHOOK_SECRET": "w",
    "AUTOREVIEWER_PRIVATE_KEY_PATH": __file__,
}


@pytest.mark.parametrize("blank", ["", " \t "])
def test_blank_prism_config_uses_boodleton(blank: str) -> None:
    env = {**APP_ENV, "BOODLETON_API_KEY": "legacy", "AUTOREVIEWER_REASONING": "conversation=low"}
    assert Settings.from_env({**env, "PRISM_CONFIG": blank}) == Settings.from_env(env)


def test_prism_shares_models_but_not_conversation_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    finder = Mock(side_effect=LocalModel)
    checker = Mock(side_effect=LocalModel)
    for role, factory in [("finder", finder), ("checker", checker)]:
        monkeypatch.setattr(f"{__name__}.{role}", factory, raising=False)
    path = tmp_path / "prism.json"
    path.write_text(json.dumps({role: f"factory:{__name__}:{role}" for role in ("finder", "checker")}))
    settings = Settings.from_env({**APP_ENV, "PRISM_CONFIG": str(path)})
    plan = ModelPlan.from_settings(settings)
    finder.assert_not_called()
    checker.assert_not_called()
    assert plan.finder("correctness")[0] is plan.finder("security")[0]
    workspace = Workspace(tmp_path, "base", "head")
    deps = conversation_deps(settings, Mock(spec=GitHubApp), plan)
    answer = deps.agents(workspace)
    verifier = deps.checkers(workspace, "correctness")
    assert answer.model is verifier.model is plan.conversation()[0]
    assert answer.model_settings == {"thinking": "high"}
    assert not verifier.model_settings
    finder.assert_called_once_with()
    checker.assert_called_once_with()
    assert ModelPlan.from_settings(settings).conversation()[0] is not answer.model
    assert checker.call_count == 2
