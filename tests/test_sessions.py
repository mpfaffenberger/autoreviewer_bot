from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.test import TestModel

from autoreviewer.review import sessions as storage
from autoreviewer.review.agents import boodleton_model
from autoreviewer.review.sessions import RoleSessions

pytestmark = pytest.mark.anyio


def sessions(path: Path, head: str = "a", base: str = "b", namespace: str = "one") -> RoleSessions:
    return RoleSessions(path, head, base, AsyncMock(return_value=False), namespace)


def transcript(text: str) -> list[ModelMessage]:
    return [
        ModelResponse(parts=[ToolCallPart("read", {}, "id")]),
        ModelRequest(parts=[ToolReturnPart("read", text, "id")]),
        ModelResponse(parts=[TextPart(text)]),
    ]


async def test_transaction_and_ancestry(tmp_path: Path) -> None:
    agent = Agent(TestModel())
    prior = sessions(tmp_path)
    seen = transcript("observed")
    staged = list(seen)
    prior.record("review", agent, staged)
    staged.clear()
    assert not list(tmp_path.iterdir())
    assert await prior.history("review", agent) == []
    prior.commit()
    check = AsyncMock(return_value=True)
    assert await RoleSessions(tmp_path, "a", "b", check, "one").history("review", agent) == seen
    check.assert_not_awaited()
    assert await RoleSessions(tmp_path, "new", "b", check, "one").history("review", agent) == seen
    check.assert_awaited_once_with("a")
    assert await sessions(tmp_path, head="fork").history("review", agent) == []


async def test_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def model(endpoint: str = "one", name: str = "model") -> Agent[None, str]:
        return Agent(
            boodleton_model(name, base_url=f"http://{endpoint}/", api_key="not-a-key", lanes=2),
            instructions="prompt",
            system_prompt="system",
        )

    saved = sessions(tmp_path)
    agent = model()
    seen = transcript("answer")
    saved.record("../role", agent, seen, subject="../subject")
    saved.commit()
    assert await saved.history("../role", model(), subject="../subject") == seen
    for other in (
        model("two"),
        model(name="other"),
        Agent(agent.model, instructions="new", system_prompt="system"),
        Agent(agent.model, instructions="prompt"),
        Agent(agent.model, instructions="prompt", system_prompt="system", output_type=int),
        Agent(agent.model, instructions="prompt", system_prompt="system", model_settings={"temperature": 0.1}),
    ):
        assert await saved.history("../role", other, subject="../subject") == []
    assert await saved.history("other", agent, subject="../subject") == []
    assert await saved.history("../role", agent) == []
    for cache in (sessions(tmp_path, base="other"), sessions(tmp_path, namespace="other")):
        assert await cache.history("../role", agent, subject="../subject") == []
    monkeypatch.setattr(storage, "_RUNTIME", ("new",))
    assert await saved.history("../role", agent, subject="../subject") == []


async def test_interrupted_publish_and_private_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = Agent(TestModel())
    first, second = sessions(tmp_path), sessions(tmp_path)
    one, two = transcript("one"), transcript("two")
    first.record("first", agent, one)
    second.record("second", agent, two)
    first.commit()
    with monkeypatch.context() as patch:

        def fail(src: Path, dst: Path) -> None:
            raise OSError("interrupted")

        patch.setattr(storage.os, "replace", fail)
        with pytest.raises(OSError, match="interrupted"):
            second.commit()
    assert await first.history("first", agent) == one
    assert not await first.history("second", agent)
    second.commit()
    assert await first.history("second", agent) == two
    assert await first.history("first", agent) == one
    for path in tmp_path.rglob("*"):
        assert path.stat().st_mode & 0o077 == 0
    (file,) = (p for p in tmp_path.rglob("*") if p.is_file())
    file.write_bytes(file.read_bytes().replace(b"one", b"bad"))
    assert not await first.history("first", agent)
    first.record("first", agent, one)
    first.commit()
    assert await first.history("first", agent) == one


async def test_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage, "_MAX_ENTRIES", 2)
    monkeypatch.setattr(storage, "_MAX_ENTRY", 3000)
    cache = sessions(tmp_path)
    agent = Agent(TestModel())
    seen = transcript("small")
    for role in ["first", "second", "third"]:
        cache.record(role, agent, seen)
        cache.commit()
    cache.record("oversize", agent, transcript("x" * 3000))
    cache.commit()
    assert not await cache.history("oversize", agent)
    assert not await cache.history("first", agent)
    assert await cache.history("second", agent) == seen
    assert await cache.history("third", agent) == seen


async def test_snapshot_byte_budget_and_parent_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from autoreviewer.review.memory import ReviewMemory

    memory = ReviewMemory(tmp_path)
    directory = memory.directory("review:org/repo#42")
    agent = Agent(TestModel())
    cache = sessions(directory)
    seen = transcript("example")
    cache.record("first", agent, seen)
    cache.commit()
    (file,) = (p for p in directory.rglob("*") if p.is_file())
    limit = file.stat().st_size + 10
    monkeypatch.setattr(storage, "_MAX_BYTES", limit)
    cache.record("second", agent, seen)
    cache.commit()
    assert file.stat().st_size <= limit
    assert not await cache.history("first", agent)
    assert await cache.history("second", agent) == seen
    memory.forget("review:org/repo#42")
    assert not directory.exists()
    assert not await cache.history("second", agent)
