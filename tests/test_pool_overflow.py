"""Overflow beside a shared model pool: overflow pins the finders, the pool still places every check."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel

from autoreviewer.config_cli import main
from autoreviewer.model_config import PrismConfig
from autoreviewer.review import agents
from autoreviewer.review.job import ReviewDeps
from autoreviewer.review.memory import ReviewMemory
from autoreviewer.review.workspace import Workspace
from autoreviewer.web.app import ModelPlan, overflow_deps
from autoreviewer.web.config import OVERFLOW_REPOS, Settings
from tests.test_prism_model_plan import APP_ENV

SHARED = "http://localhost:8000/v1"


def test_cli_local_url_fills_overflow_and_pool(tmp_path: Path) -> None:
    path = tmp_path / "prism.json"
    main(
        [
            *("--output", str(path), "--local-url", SHARED, "--overflow", "local:burst"),
            *("--pool-model", "local:a=2", "--pool-model", "openai:b=1"),
        ]
    )
    config = PrismConfig.from_file(path)
    assert (config.finder, config.checker, config.overflow) == ("local:a", "openai:b", "local:burst")
    assert config.pool == {"local:a": 2, "openai:b": 1}
    assert config.endpoints == {"local:a": SHARED, "local:burst": SHARED}


def test_overflow_lanes_with_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    capped: list[tuple[str, int]] = []
    real = agents.with_lanes
    monkeypatch.setattr(
        agents, "with_lanes", lambda model, name, lanes: capped.append((name, lanes)) or real(model, name, lanes)
    )
    pool = {"openai:a": 2, "openai:b": 3, "openai:c": 4}
    member = PrismConfig.model_validate({"pool": pool, "overflow": "openai:c"})
    # A pool member's capacity is the model's cap, whichever role uses it.
    assert isinstance(member.resolve("openai:c")[0], ConcurrencyLimitedModel) and capped == [("openai:c", 4)]
    outside = PrismConfig.model_validate({"pool": pool, "overflow": "openai:burst"})
    assert not isinstance(outside.resolve("openai:burst")[0], ConcurrencyLimitedModel)
    with pytest.raises(ValidationError, match="overflow finder and checker"):
        PrismConfig.model_validate({"pool": pool, "overflow": "openai:b"})  # b is the pool's default checker


@pytest.mark.parametrize("spec", ["openai:burst", "openai:c"])
def test_overflow_finders_with_pool_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spec: str) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    path = tmp_path / "prism.json"
    path.write_text(json.dumps({"pool": {"openai:a": 1, "openai:b": 1, "openai:c": 1}, "overflow": spec}))
    settings = Settings.from_env({**APP_ENV, "PRISM_CONFIG": str(path), OVERFLOW_REPOS: "org/repo"})
    plan = ModelPlan.from_settings(settings)
    assert plan.pool
    select = Mock()
    normal = ReviewDeps(
        Mock(), Mock(), Mock(), tmp_path, "bot", "normal", memory=ReviewMemory(tmp_path), select_correctness=select
    )
    overflow = overflow_deps(settings, normal, plan)
    assert overflow and overflow.select_correctness is None and overflow.session_namespace == "overflow"
    assert spec in overflow.models_note and "openai:a" in overflow.models_note
    workspace = Workspace(tmp_path, "base", "head")
    assert overflow.simplifier
    simplify = overflow.simplifier(workspace)
    pairs = [overflow.agents(workspace), *(lens.agents(workspace) for lens in overflow.lenses)]
    for finder, check in [*pairs, (simplify.propose, simplify.check)]:
        assert finder.model and finder.model.model_name == spec.split(":", 1)[1]
        checker = check()
        assert checker.model and checker.model.model_name in {"a", "b", "c"} - {finder.model.model_name}
    select.assert_not_called()
