"""A finding withdrawn in conversation stops blocking: decided by code from the PR's comments, never by a model."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoreviewer.github import Repo
from autoreviewer.review.job import review_and_post
from autoreviewer.review.pipeline import Outcome
from autoreviewer.review.posting import (
    WITHDRAWN_MARKER,
    Publisher,
    answered_marker,
    post_review,
    reconsider,
    withdrawn_placements,
)
from autoreviewer.review.schemas import Finding
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import BOT, FakeGitHub, GitRepo, finding, job_deps

pytestmark = pytest.mark.anyio

REPO = Repo(1, "mpfaffenberger", "demo")


def blocking(line: int = 5, *, kind: str = "correctness", priority: int = 1) -> Finding:
    return Finding.model_validate({**finding(line, priority=priority), "kind": kind})


async def reviewed(repo: GitRepo, *findings: Finding, general: tuple[Finding, ...] = ()) -> FakeGitHub:
    """A PR whose head was reviewed: each inline finding posted as its own comment (ids 1, 2, ...), then the
    verdict."""
    github = FakeGitHub(repo)
    publish = Publisher(github, REPO, 7, head_sha=repo.head, author=BOT, block_at=1)
    for f in findings:
        await publish(f)
    outcome = Outcome(inline=list(findings), general=list(general))
    await post_review(github, REPO, 7, head_sha=repo.head, author=BOT, models_note="m", outcome=outcome,
                      posted=publish.posted)  # fmt: skip
    return github


def withdraw(github: FakeGitHub, root: int, *, withdrawn: bool = True) -> None:
    """Someone pushes back in the thread under comment `root`, and the bot answers."""
    comment = github.say("alice", root, "This can't happen.")
    github.say(BOT, root, f"Checked.\n\n{answered_marker(comment)}" + (f"\n{WITHDRAWN_MARKER}" if withdrawn else ""))


async def test_verdicts_record_their_state_and_what_they_listed_outside_the_diff(repo: GitRepo) -> None:
    github = await reviewed(repo, blocking(5), general=(blocking(99),))
    assert "<!-- autoreviewer:state REQUEST_CHANGES outside=1 -->" in github.reviews[-1]["body"]


@pytest.mark.parametrize(("answers", "withdrawn"), [([True], True), ([True, False], False), ([False, True], True)])
async def test_the_latest_answer_in_a_thread_decides(repo: GitRepo, answers: list[bool], withdrawn: bool) -> None:
    github = await reviewed(repo, blocking(5))
    for answer in answers:
        withdraw(github, 1, withdrawn=answer)
    placements = withdrawn_placements(await github.posted_comments(REPO, 7, author=BOT))
    assert placements == ({(blocking(5).path, 5, "correctness")} if withdrawn else set())


async def test_an_outdated_comment_withdraws_nothing(repo: GitRepo) -> None:
    """The code under it changed: the finding is judged afresh."""
    github = await reviewed(repo, blocking(5))
    withdraw(github, 1)
    github.review_comments[0]["outdated"] = True
    assert withdrawn_placements(await github.posted_comments(REPO, 7, author=BOT)) == frozenset()


async def test_a_withdrawn_finding_found_again_on_the_next_push_doesnt_block(repo: GitRepo) -> None:
    github = await reviewed(repo, blocking(5))
    withdraw(github, 1)
    publish = Publisher(github, REPO, 7, head_sha="b" * 40, author=BOT, block_at=1)
    await publish(blocking(5))  # found again: already on the PR, so not posted again
    outcome = Outcome(inline=[blocking(5)], withdrawn=await publish.withdrawn())
    assert not outcome.blocking
    await post_review(github, REPO, 7, head_sha="b" * 40, author=BOT, models_note="m", outcome=outcome,
                      posted=publish.posted)  # fmt: skip
    assert github.reviews[-1]["event"] == "APPROVE"


async def test_approves_once_nothing_blocking_is_left_standing(repo: GitRepo) -> None:
    github = await reviewed(repo, blocking(5), blocking(9, kind="dry"))  # DRY never blocks
    withdraw(github, 1)
    assert await reconsider(github, REPO, 7, author=BOT, block_at=1)
    assert github.reviews[-1]["event"] == "APPROVE"
    assert github.checks[-1]["conclusion"] == "success"
    assert not await reconsider(github, REPO, 7, author=BOT, block_at=1)  # already approved: a replay is a no-op


async def test_a_finding_still_standing_keeps_the_block(repo: GitRepo) -> None:
    github = await reviewed(repo, blocking(5), blocking(9))
    withdraw(github, 1)
    assert not await reconsider(github, REPO, 7, author=BOT, block_at=1)
    assert [r["event"] for r in github.reviews] == ["REQUEST_CHANGES"]


async def test_a_verdict_with_blocking_findings_outside_the_diff_is_never_overturned(repo: GitRepo) -> None:
    """Those have no thread to argue in."""
    github = await reviewed(repo, blocking(5), general=(blocking(99),))
    withdraw(github, 1)
    assert not await reconsider(github, REPO, 7, author=BOT, block_at=1)


async def test_a_verdict_from_before_state_markers_is_never_overturned(repo: GitRepo) -> None:
    github = await reviewed(repo, blocking(5))
    github.reviews[-1]["body"] = github.reviews[-1]["body"].replace("<!-- autoreviewer:state", "<!-- old")
    withdraw(github, 1)
    assert not await reconsider(github, REPO, 7, author=BOT, block_at=1)


@pytest.mark.parametrize(("withdrawn", "event"), [(True, "APPROVE"), (False, "REQUEST_CHANGES")])
async def test_the_next_review_of_the_pr_counts_withdrawals(
    repo: GitRepo, tmp_path: Path, withdrawn: bool, event: str
) -> None:
    """The bot's own job: the same defect found again on unchanged code blocks only if it wasn't withdrawn."""
    github = FakeGitHub(repo)
    await Publisher(github, REPO, 7, head_sha="a" * 40, author=BOT, block_at=1)(blocking(5))  # an earlier head
    withdraw(github, 1, withdrawn=withdrawn)
    deps = job_deps(github, tmp_path, [finding(5)])
    await review_and_post(deps, PullRequestUpdated(1, "mpfaffenberger", "demo", 7, repo.head))
    assert github.reviews[-1]["event"] == event
