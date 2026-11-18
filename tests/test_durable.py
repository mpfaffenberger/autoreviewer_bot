"""DBOS durability: queued composition, one review per head, and recovery after a real process crash."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from dbos import DBOS

from autoreviewer.review import durable
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import FakeGitHub, GitRepo, finding, job_deps, make_repo

pytestmark = pytest.mark.anyio

HEAD = "a" * 40
PULL = PullRequestUpdated(installation_id=1, owner="mpfaffenberger", repo="demo", number=7, head_sha=HEAD)


def pull_for(repo: GitRepo) -> PullRequestUpdated:
    return PullRequestUpdated(installation_id=1, owner="mpfaffenberger", repo="demo", number=7, head_sha=repo.head)


CRASH_SCRIPT = Path(__file__).with_name("durable_crash_script.py")

Launch = Callable[[list[dict[str, Any]]], Awaitable[FakeGitHub]]


@pytest.fixture
async def launch(tmp_path: Path) -> AsyncIterator[Launch]:
    async def start(findings: list[dict[str, Any]]) -> FakeGitHub:
        github = FakeGitHub(make_repo(tmp_path / "origin"))
        durable.configure(tmp_path / "dbos.sqlite")
        await durable.launch(job_deps(github, tmp_path, findings), concurrency=2)
        return github

    yield start
    durable.shutdown()


async def test_a_queued_review_runs_all_three_phases(launch: Launch) -> None:
    github = await launch([finding(5)])

    result = await (await durable.enqueue(pull_for(github.repo))).get_result()

    assert result.conclusion == "failure"
    assert [c["status"] for c in github.checks] == ["completed"]
    assert github.checks[0]["title"] == "1 blocking issue(s)"
    assert [r["event"] for r in github.reviews] == ["REQUEST_CHANGES"]


async def test_duplicate_events_for_one_head_produce_one_review(launch: Launch) -> None:
    github = await launch([finding(5)])

    handles = [await durable.enqueue(pull_for(github.repo)) for _ in range(3)]  # e.g. GitHub redeliveries
    for handle in handles:
        await handle.get_result()

    assert len({handle.workflow_id for handle in handles}) == 1
    assert len(github.checks) == 1 and len(github.reviews) == 1


async def test_enqueue_review_says_whether_the_head_was_already_reviewed(launch: Launch) -> None:
    github = await launch([])
    pull = pull_for(github.repo)
    assert await durable.enqueue_review(pull) is False  # queued: a new review runs
    await (await durable.enqueue(pull)).get_result()
    assert await durable.enqueue_review(pull) is True  # finished: asking again runs nothing
    assert len(github.reviews) == 1


async def test_only_an_explicit_request_retries_a_crashed_review(launch: Launch) -> None:
    github = await launch([])
    github.fail_start_check = True
    pull = pull_for(github.repo)
    handle = await durable.enqueue(pull)
    with pytest.raises(Exception, match="maximum of 3 retries"):
        await handle.get_result()
    attempts = github.check_attempts
    assert await durable.enqueue_review(pull) is False  # e.g. a redelivered push: left alone
    await asyncio.sleep(3)  # long enough for the queue (polled about once a second) to start a retry
    assert (await handle.get_status()).status == "ERROR" and github.check_attempts == attempts

    github.fail_start_check = False
    assert await durable.enqueue_review(pull, retry_failed=True) is False  # "@bot review": runs again
    for _ in range(200):
        if github.reviews:
            break
        await asyncio.sleep(0.05)
    assert [r["event"] for r in github.reviews] == ["APPROVE"]


async def test_an_explicit_request_resumes_a_cancelled_review_on_its_check_run(launch: Launch) -> None:
    github = await launch([])
    github.check_update_delay = 0
    pull = pull_for(github.repo)
    handle = await durable.enqueue(pull)
    await DBOS.cancel_workflow_async(handle.workflow_id)
    assert (await handle.get_status()).status == "CANCELLED"
    await durable.enqueue_review(pull, retry_failed=True)
    await (await DBOS.retrieve_workflow_async(handle.workflow_id)).get_result()
    assert [r["event"] for r in github.reviews] == ["APPROVE"] and len(github.checks) == 1


async def test_an_explicit_request_reruns_a_review_that_ended_without_a_verdict(launch: Launch) -> None:
    github = await launch([])
    github.fail_get_pull = True  # the review fails: "Review failed", a neutral check and no verdict
    pull = pull_for(github.repo)
    result = await (await durable.enqueue(pull)).get_result()
    assert result.title == "Review failed" and github.reviews == []
    assert await durable.enqueue_review(pull) is True  # a redelivered push: finished, left alone

    github.fail_get_pull = False
    assert await durable.enqueue_review(pull, retry_failed=True) is False
    for _ in range(200):
        if github.reviews:
            break
        await asyncio.sleep(0.05)
    assert [r["event"] for r in github.reviews] == ["APPROVE"] and len(github.checks) == 1  # same check run


def test_workflow_ids_are_per_head_and_case_insensitive() -> None:
    other_head = PullRequestUpdated(1, "mpfaffenberger", "demo", 7, "b" * 40)
    shouted = PullRequestUpdated(1, "MPFAFFENBERGER", "Demo", 7, HEAD)
    assert durable.workflow_id(PULL) != durable.workflow_id(other_head)
    assert durable.workflow_id(PULL) == durable.workflow_id(shouted)


def test_a_review_interrupted_by_a_crash_finishes_after_restart_exactly_once(tmp_path: Path) -> None:
    """Process 1 dies (os._exit) inside the review; process 2 recovers it. One check run, one review."""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}

    def run(mode: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CRASH_SCRIPT), str(tmp_path), mode],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    first = run("enqueue-and-crash")
    assert first.returncode == 9, first.stderr[-2000:]
    assert json.loads((tmp_path / "github.json").read_text())["reviews"] == []  # died before posting

    second = run("recover")
    assert second.returncode == 0, second.stderr[-2000:]

    state = json.loads((tmp_path / "github.json").read_text())
    assert [c["status"] for c in state["checks"]] == ["completed"], "the check run must be created once and finished"
    assert [r["event"] for r in state["reviews"]] == ["REQUEST_CHANGES"]


def test_old_sqlite_is_refused_with_a_clear_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(durable.sqlite3, "sqlite_version_info", (3, 40, 1))
    monkeypatch.setattr(durable.sqlite3, "sqlite_version", "3.40.1")
    with pytest.raises(RuntimeError, match=r"SQLite 3\.40\.1 is too old for DBOS"):
        durable.configure(tmp_path / "dbos.sqlite")
