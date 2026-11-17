"""Live progress: what the agents did, safe to show on a check run."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart

from autoreviewer.review import job
from autoreviewer.review.job import _publish, finish_check, review_and_post, start_check
from autoreviewer.review.pipeline import review
from autoreviewer.review.progress import Progress
from autoreviewer.review.workspace import Workspace
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import FakeGitHub, GitRepo, agents, finding, job_deps
from tests.test_review import pr_text

pytestmark = pytest.mark.anyio

RESPONSE = ModelResponse(
    parts=[
        ThinkingPart("the caller passes an empty list, so len(xs) is zero"),
        TextPart("Let me read the callers."),
        ToolCallPart("diff", {"path": "stats.py"}),
        ToolCallPart("final_result", {"findings": []}),
    ]
)


def test_records_thinking_text_and_tool_calls() -> None:
    progress = Progress()
    progress.set_phase("Reviewing the change")
    progress.record("reviewer", RESPONSE)
    assert (progress.requests, progress.tool_calls) == (1, 2)
    assert progress.summary().startswith("Reviewing the change: 1 model requests, 2 tool calls")
    details = progress.details()
    assert "reviewer: …the caller passes an empty list" in details
    assert details.index("reviewer → final report") < details.index("reviewer → diff(path='stats.py')")  # newest first
    assert "reviewer says: Let me read the callers." in details


def test_model_text_cannot_ping_leak_or_break_out_of_its_code_block() -> None:
    progress = Progress()
    planted = "```\n# Injected heading\ncc @mpfaffenberger, token ghp_" + "a" * 36
    progress.record("reviewer", ModelResponse(parts=[TextPart(planted)]))
    details = progress.details()
    assert details.count("```") == 2  # only our own fence: the planted one is defused
    assert "@\u200bmpfaffenberger" in details and "ghp_aaaa" not in details


async def test_the_review_reports_its_phases_and_verdicts(workspace: Workspace) -> None:
    progress = Progress()
    lens, verifier = agents([finding(5)])(workspace)
    await review(await pr_text(workspace), lens, verifier, progress=progress)
    details = progress.details()
    assert "Verifying 1 candidate finding(s)" in details
    assert "reviewer → final report" in details and "confirmed: Mean is off by one" in details
    assert "verifier (stats.py:5) → final report" in details


async def test_publishes_only_changes_and_survives_refusals(
    repo: GitRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job, "PROGRESS_INTERVAL_S", 0.01)
    github = FakeGitHub(repo, fail_check_updates=True)
    deps, progress = job_deps(github, tmp_path), Progress()
    publisher = asyncio.create_task(_publish(deps, job.repo_of(pull(repo)), 1, progress))
    progress.note("first")
    await asyncio.sleep(0.05)  # refused: logged, and the publisher carries on
    github.fail_check_updates = False
    progress.note("second")
    await asyncio.sleep(0.05)
    publisher.cancel()
    assert len(github.check_updates) == 1 and "second" in github.check_updates[0]["text"]


async def test_the_finished_check_run_keeps_the_trail(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo)
    deps = job_deps(github, tmp_path, [finding(5)])
    check_id = await start_check(deps, pull(repo))
    await finish_check(deps, pull(repo), check_id, await review_and_post(deps, pull(repo), check_id))
    assert "reviewer → final report" in github.checks[0]["text"]


def pull(repo: GitRepo) -> PullRequestUpdated:
    return PullRequestUpdated(installation_id=1, owner="mpfaffenberger", repo="demo", number=7, head_sha=repo.head)
