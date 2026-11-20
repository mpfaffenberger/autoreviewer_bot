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
    "PRISM_PRIVATE_KEY_PATH": __file__,
}


@pytest.mark.parametrize("blank", ["", " \t "])
def test_blank_prism_config_uses_boodleton(blank: str) -> None:
    env = {**APP_ENV, "BOODLETON_API_KEY": "legacy", "PRISM_REASONING": "conversation=low"}
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


def test_receiver_reads_migrated_paths(tmp_path: Path) -> None:
    key = tmp_path / "app.pem"
    key.write_text("test key")
    settings = Settings.from_env(
        {
            **APP_ENV,
            "BOODLETON_API_KEY": "test",
            "PRISM_PRIVATE_KEY_PATH": str(key),
            "PRISM_WORK_DIR": str(tmp_path / "checkout"),
            "PRISM_DATABASE": str(tmp_path / "jobs.sqlite"),
            "PRISM_STEPS_DIR": str(tmp_path / "history"),
            "PRISM_MAX_CONCURRENT_JOBS": "3",
        }
    )
    assert settings.private_key == "test key"
    assert settings.work_dir == tmp_path / "checkout"
    assert settings.database == tmp_path / "jobs.sqlite"
    assert settings.steps_dir == tmp_path / "history"
    assert settings.max_concurrent_jobs == 3
