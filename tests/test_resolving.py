"""The bot resolves the threads of findings the code has moved past (Mike, Nov 19), and only those."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from autoreviewer.review import job
from autoreviewer.review.job import Lens, review_and_post
from autoreviewer.review.lint import LintError
from autoreviewer.review.pipeline import Outcome
from autoreviewer.review.posting import Publisher, resolve_fixed
from autoreviewer.review.schemas import Finding
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import BOT, FakeGitHub, GitRepo, finding, job_deps, lens_agents
from tests.test_withdrawals import REPO, blocking, reviewed

pytestmark = pytest.mark.anyio

NEXT_HEAD = "c" * 40


async def pushed_past(repo: GitRepo, *findings: Finding) -> FakeGitHub:
    """A PR whose findings were posted on one head, and whose next push changed the code under every one."""
    github = await reviewed(repo, *findings)
    for comment in github.review_comments:
        comment["outdated"] = True
    return github


CHECKED = Outcome(covered={"correctness"})
"""A review that checked correctness in full and reported nothing."""


async def resolve(github: FakeGitHub, outcome: Outcome = CHECKED) -> list[int]:
    return await resolve_fixed(github, REPO, 7, head_sha=NEXT_HEAD, author=BOT, outcome=outcome)


async def test_a_finding_the_review_found_nothing_like_is_resolved_with_a_note(repo: GitRepo) -> None:
    github = await pushed_past(repo, blocking(5), blocking(9, title="Division by zero on an empty list"))
    assert await resolve(github) == [1, 2]
    assert github.resolved_threads == {"T1", "T2"}
    notes = [c["comment"].body for c in github.review_comments if c.get("in_reply_to") is not None]
    note = f"The code here changed, and the review of `{NEXT_HEAD[:7]}` found nothing like this in the file: resolving."
    assert notes == [note] * 2


@pytest.mark.parametrize(
    "outcome",
    [
        replace(CHECKED, inline=[blocking(7)]),  # found again, on a line that moved
        replace(CHECKED, inline=[blocking(7, title="Mean divides by the wrong count")]),  # reworded
        replace(CHECKED, general=[blocking(30, title="Division by zero on an empty list")]),  # another, same file
        replace(CHECKED, unverified=[(blocking(7), "checker timed out")]),  # not yet confirmed
        replace(CHECKED, rejected=[(blocking(7), "not a bug")]),  # re-reported, then rejected
        replace(CHECKED, low_confidence=[blocking(7)]),  # re-reported as a hunch (the bot's review of #42)
        Outcome(),  # correctness wasn't checked in full: failed, cut short (the bot's review of #42)
        Outcome(covered={"security"}),
    ],
)
async def test_a_finding_that_may_still_be_there_stays_open(repo: GitRepo, outcome: Outcome) -> None:
    github = await pushed_past(repo, blocking(5))
    assert await resolve(github, outcome) == []
    assert github.resolved_threads == set()


@pytest.mark.parametrize(
    "reported",
    [
        blocking(5, kind="security"),  # another kind, same place
        Finding.model_validate({**finding(5), "path": "other.py"}),  # same kind, another file
    ],
)
async def test_only_the_same_kind_in_the_same_file_keeps_it_open(repo: GitRepo, reported: Finding) -> None:
    github = await pushed_past(repo, blocking(5))
    assert await resolve(github, replace(CHECKED, inline=[reported])) == [1]


async def test_a_comment_still_on_the_heads_code_stays_open(repo: GitRepo) -> None:
    """Not outdated: the code under it didn't change, so the push didn't fix it, whatever this review found."""
    github = await reviewed(repo, blocking(5))
    assert await resolve(github) == []


async def test_a_resolved_thread_gets_no_second_note(repo: GitRepo) -> None:
    """The author resolved it, or an earlier review did: replays and later pushes leave it alone."""
    github = await pushed_past(repo, blocking(5))
    github.resolved_threads.add("T1")
    assert await resolve(github) == []
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


ELSEWHERE = Finding.model_validate({**finding(3), "kind": "dry", "path": "notes.py"}).model_dump()


@pytest.mark.parametrize(
    ("lens", "resolved"),
    [
        (Lens("dry", lens_agents([], kind="dry")), {"T1"}),  # finished, found nothing like it
        (Lens("dry", lens_agents([], kind="dry", crash=True)), set()),  # failed: it can't say (#42's review)
        (Lens("dry", lens_agents([ELSEWHERE], kind="dry"), max_posted=1), set()),  # at its cap: may have dropped it
        (Lens("dry", lens_agents([ELSEWHERE], kind="dry"), max_posted=2), {"T1"}),  # under its cap
    ],
)
async def test_the_review_resolves_only_what_it_checked_in_full(
    repo: GitRepo, tmp_path: Path, lens: Lens, resolved: set[str]
) -> None:
    github = FakeGitHub(repo)
    await Publisher(github, REPO, 7, head_sha="a" * 40, author=BOT, block_at=1)(blocking(5, kind="dry"))
    github.review_comments[0]["outdated"] = True
    deps = replace(job_deps(github, tmp_path), lenses=(lens,))
    await review_and_post(deps, PullRequestUpdated(1, "mpfaffenberger", "demo", 7, repo.head))
    assert github.resolved_threads == resolved


@pytest.mark.parametrize("ruff_fails", [False, True])
async def test_a_linters_finding_is_resolved_only_when_the_linter_ran(
    repo: GitRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ruff_fails: bool
) -> None:
    github = FakeGitHub(repo)
    await Publisher(github, REPO, 7, head_sha="a" * 40, author=BOT, block_at=1)(blocking(5, kind="types"))
    github.review_comments[0]["outdated"] = True
    if ruff_fails:

        async def broken(*_: object) -> list[Finding]:
            raise LintError("ruff: no such file")

        monkeypatch.setattr(job, "lint_findings", broken)
    await review_and_post(job_deps(github, tmp_path), PullRequestUpdated(1, "mpfaffenberger", "demo", 7, repo.head))
    assert github.resolved_threads == (set() if ruff_fails else {"T1"})
