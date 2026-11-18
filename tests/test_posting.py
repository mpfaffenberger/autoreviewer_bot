"""Findings are posted the moment they're confirmed: once each, even across replays."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoreviewer.github import Repo
from autoreviewer.review.agents import REPORT_TOOL, correctness_agent, verifier_agent
from autoreviewer.review.job import review_and_post
from autoreviewer.review.pipeline import review
from autoreviewer.review.posting import Publisher, fingerprint
from autoreviewer.review.schemas import Finding
from autoreviewer.review.workspace import Workspace
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import FakeGitHub, GitRepo, finding, job_deps, verifier_model
from tests.test_review import pr_text

pytestmark = pytest.mark.anyio

REPO = Repo(1, "mpfaffenberger", "demo")


def confirmed(line: int = 5, *, kind: str = "correctness", priority: int = 1) -> Finding:
    return Finding.model_validate({**finding(line, priority=priority), "kind": kind})


def publisher(github: FakeGitHub) -> Publisher:
    return Publisher(github, REPO, 7, head_sha=github.repo.head, author="chaos-emerald-reviewer[bot]", block_at=1)


async def test_each_finding_is_posted_once_even_when_a_review_is_replayed(repo: GitRepo) -> None:
    github = FakeGitHub(repo)
    await publisher(github)(confirmed())
    replayed = publisher(github)  # a fresh process recovering the same review
    await replayed(confirmed())
    assert len(github.review_comments) == 1
    assert replayed.posted == {fingerprint(repo.head, confirmed())}  # so the final review won't repeat it


@pytest.mark.parametrize(
    ("first", "second", "posted"),
    [
        (confirmed(kind="security", priority=1), confirmed(priority=2), 1),  # less urgent repeat: skipped
        (confirmed(priority=2), confirmed(kind="security", priority=1), 2),  # escalation: posted too
        (confirmed(), confirmed(kind="dry", priority=1), 2),  # a different kind of issue
    ],
)
async def test_one_defect_seen_by_two_lenses_is_posted_once_unless_it_escalates(
    repo: GitRepo, first: Finding, second: Finding, posted: int
) -> None:
    github = FakeGitHub(repo)
    publish = publisher(github)
    await publish(first)
    await publish(second)
    assert len(github.review_comments) == posted


async def test_a_failed_post_goes_in_the_final_review(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo, fail_review_comments=True)
    await review_and_post(job_deps(github, tmp_path, [finding(5)]), _pull(repo))
    assert github.review_comments == [] and [(c.path, c.line) for c in github.reviews[0]["comments"]] == [
        ("stats.py", 5)
    ]


async def test_findings_posted_early_are_not_repeated_in_the_verdict(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo)
    await review_and_post(job_deps(github, tmp_path, [finding(5)]), _pull(repo))
    assert len(github.review_comments) == 1 and github.reviews[0]["comments"] == []
    assert github.review_comments[0]["comment"].body.endswith("-->")  # carries its fingerprint


def _pull(repo: GitRepo) -> PullRequestUpdated:
    return PullRequestUpdated(installation_id=1, owner="mpfaffenberger", repo="demo", number=7, head_sha=repo.head)


async def test_a_finding_is_posted_while_the_lens_is_still_reviewing(repo: GitRepo, workspace: Workspace) -> None:
    github, verified, seen_mid_run = FakeGitHub(repo), list[int](), list[int]()

    async def lens(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, "parts", [])):
            return ModelResponse(parts=[ToolCallPart(REPORT_TOOL, {"finding": finding(5)})])
        for _ in range(100):  # the lens keeps "reviewing" until the comment shows up, or gives up
            if github.review_comments:
                break
            await asyncio.sleep(0.01)
        seen_mid_run.append(len(github.review_comments))
        report = {"findings": [finding(5)], "overall_correct": False, "reviewed": ["stats.py"]}  # repeated at the end
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, report)])

    def confirm(reported: dict[str, object]) -> bool:
        verified.append(int(str(reported["line"])))
        return True

    agent = correctness_agent(FunctionModel(lens), workspace)
    publish = Publisher(github, REPO, 7, head_sha=repo.head, author="chaos-emerald-reviewer[bot]", block_at=1)
    outcome = await review(
        await pr_text(workspace), agent, verifier_agent(verifier_model(confirm), workspace), publish=publish
    )
    assert seen_mid_run == [1]  # posted before the lens finished
    assert verified == [5]  # reported mid-run and again at the end: verified once
    assert [f.line for f in outcome.posted] == [5]
