"""Exercise synchronous placement against the same lanes used by actual requests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoreviewer import model_pool
from autoreviewer.model_config import PrismConfig
from autoreviewer.model_pool import Choice, ModelPool
from autoreviewer.review.agents import _lanes, with_lanes  # pyright: ignore[reportPrivateUsage]


def config(
    monkeypatch: pytest.MonkeyPatch, capacities: dict[str, int], gate: asyncio.Event | None = None
) -> PrismConfig:
    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if gate is not None:
            await gate.wait()
        return ModelResponse(parts=[TextPart("ok")])

    def resolve(self: PrismConfig, spec: str) -> tuple[Model, int]:
        name = spec.split(":")[1]
        model = FunctionModel(respond, model_name=name)
        return with_lanes(model, spec, self.lane_count(spec)), self.compact_at[spec]

    monkeypatch.setattr(PrismConfig, "resolve", resolve)
    return PrismConfig.model_validate(
        {"pool": capacities, "compact_at": {s: 100 + i for i, s in enumerate(capacities)}}
    )


def pair(pool: ModelPool) -> tuple[Choice, Choice]:
    finder = pool.choose()
    return finder, pool.choose(excluding=finder[0].model_name)


def test_burst_before_requests_and_unused_pair_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1.0
    monkeypatch.setattr(model_pool, "monotonic", lambda: now)
    pool = ModelPool(config(monkeypatch, {"x:a": 2, "x:b": 1, "x:c": 1}))

    def placement() -> tuple[tuple[str, int], tuple[str, int]]:
        (f, ft), (c, ct) = pair(pool)
        return (f.model_name, ft), (c.model_name, ct)

    assert placement() == (("a", 100), ("b", 101))
    assert placement() == (("a", 100), ("c", 102))
    assert placement() == (("b", 101), ("a", 100))
    now += 1
    assert placement() == (("a", 100), ("b", 101))


def test_verifier_excludes_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = ModelPool(config(monkeypatch, {"x:a": 1, "y:a": 1, "x:b": 1}))
    for _ in range(10):
        (finder, _), (checker, _) = pair(pool)
        assert finder.model_name != checker.model_name
    with pytest.raises(ValueError, match="independent"):
        ModelPool(config(monkeypatch, {"x:a": 1, "y:a": 1}))


@pytest.mark.anyio
async def test_live_queue_and_cross_role_lane_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1.0
    monkeypatch.setattr(model_pool, "monotonic", lambda: now)
    gate = asyncio.Event()
    pool = ModelPool(config(monkeypatch, {"load:a": 1, "load:b": 1}, gate))
    (first, _), (other, _) = pair(pool)
    _, (checker, _) = pair(pool)
    assert first.model_name == checker.model_name == "a"
    # Saturate both resources, with an extra verifier queued on the first.
    async with asyncio.timeout(5), asyncio.TaskGroup() as group:
        for model in (first, other, checker):
            group.create_task(model.request([], None, ModelRequestParameters()))
        lanes = _lanes("load:a", 1)
        while not lanes.waiting_count or not _lanes("load:b", 1).running_count:
            await asyncio.sleep(0)
        assert lanes.running_count == lanes.waiting_count == 1
        now += 1  # only real request load can explain the choice
        assert pair(pool)[0][0].model_name == "b"
        gate.set()
    now += 1
    assert pair(pool)[0][0].model_name == "a"


def test_review_selects_checkers_at_verification_not_finder_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from autoreviewer.review.pipeline import select_checker
    from autoreviewer.review.workspace import Workspace
    from autoreviewer.web.app import select_correctness

    pool = ModelPool(config(monkeypatch, {"integration:a": 5, "integration:b": 1, "integration:c": 1}))
    factory, version = select_correctness(pool)
    finder, check = factory(Workspace(tmp_path, "base", "head"))
    assert version and isinstance(finder.model, Model) and finder.model.model_name == "a"
    assert callable(check)
    first, second = select_checker(check).model, select_checker(check).model
    assert isinstance(first, Model) and isinstance(second, Model)
    assert (first.model_name, second.model_name) == ("b", "c")
