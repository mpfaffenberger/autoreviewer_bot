"""The bot resolves the threads of findings the code has moved past (Mike, Nov 19), and only those."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoreviewer.review.job import review_and_post
from autoreviewer.review.pipeline import Outcome
from autoreviewer.review.posting import Publisher, resolve_fixed
from autoreviewer.review.schemas import Finding
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import BOT, FakeGitHub, GitRepo, job_deps
from tests.test_withdrawals import REPO, blocking, reviewed

pytestmark = pytest.mark.anyio

NEXT_HEAD = "c" * 40


async def pushed_past(repo: GitRepo, *findings: Finding) -> FakeGitHub:
    """A PR whose findings were posted on one head, and whose next push changed the code under every one."""
    github = await reviewed(repo, *findings)
    for comment in github.review_comments:
        comment["outdated"] = True
    return github


async def resolve(github: FakeGitHub, outcome: Outcome) -> list[int]:
    return await resolve_fixed(github, REPO, 7, head_sha=NEXT_HEAD, author=BOT, outcome=outcome)


async def test_a_finding_not_found_again_is_resolved_with_a_note(repo: GitRepo) -> None:
    github = await pushed_past(repo, blocking(5), blocking(9, title="Division by zero on an empty list"))
    assert await resolve(github, Outcome()) == [1, 2]
    assert github.resolved_threads == {"T1", "T2"}
    notes = [c["comment"].body for c in github.review_comments if c.get("in_reply_to") is not None]
    assert (
        notes == [f"The code here changed, and the review of `{NEXT_HEAD[:7]}` didn't find this again: resolving."] * 2
    )


@pytest.mark.parametrize(
    "outcome",
    [
        Outcome(inline=[blocking(7)]),  # found again, on a line that moved
        Outcome(general=[blocking(7, title="MEAN is  off by one")]),  # case and spacing aside, outside the diff
        Outcome(unverified=[(blocking(7), "checker timed out")]),  # found again, not yet confirmed
        Outcome(cut_short=True),  # the review may not have got that far
    ],
)
async def test_a_finding_that_may_still_be_there_stays_open(repo: GitRepo, outcome: Outcome) -> None:
    github = await pushed_past(repo, blocking(5))
    assert await resolve(github, outcome) == []
    assert github.resolved_threads == set()


@pytest.mark.parametrize(
    "found_again",
    [
        blocking(7, kind="security"),  # another kind
        blocking(7, title="Division by zero on an empty list"),  # another defect
    ],
)
async def test_only_the_same_finding_keeps_it_open(repo: GitRepo, found_again: Finding) -> None:
    github = await pushed_past(repo, blocking(5))
    assert await resolve(github, Outcome(inline=[found_again])) == [1]


async def test_a_comment_still_on_the_heads_code_stays_open(repo: GitRepo) -> None:
    """Not outdated: the code under it didn't change, so the push didn't fix it, whatever this review found."""
    github = await reviewed(repo, blocking(5))
    assert await resolve(github, Outcome()) == []


async def test_a_resolved_thread_gets_no_second_note(repo: GitRepo) -> None:
    """The author resolved it, or an earlier review did: replays and later pushes leave it alone."""
    github = await pushed_past(repo, blocking(5))
    github.resolved_threads.add("T1")
    assert await resolve(github, Outcome()) == []
    assert len(github.review_comments) == 1


@pytest.mark.parametrize("threads_fail", [False, True])
async def test_the_bots_review_resolves_them_and_never_fails_for_it(
    repo: GitRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, threads_fail: bool
) -> None:
    """The job resolves after its verdict; if GitHub's threads can't be read, the review stands all the same."""
    github = FakeGitHub(repo)
    await Publisher(github, REPO, 7, head_sha="a" * 40, author=BOT, block_at=1)(blocking(5))  # an earlier head
    github.review_comments[0]["outdated"] = True  # the push changed the code under it
    if threads_fail:

        async def unreadable(*_: object) -> None:
            raise RuntimeError("502 Bad Gateway")

        monkeypatch.setattr(github, "review_threads", unreadable)
    pull = PullRequestUpdated(1, "mpfaffenberger", "demo", 7, repo.head)
    result = await review_and_post(job_deps(github, tmp_path, []), pull)
    assert (result.conclusion, github.reviews[-1]["event"]) == ("success", "APPROVE")
    assert github.resolved_threads == (set() if threads_fail else {"T1"})
