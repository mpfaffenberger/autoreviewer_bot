"""`@example-reviewer review`: queue the push path's review of the current head, and acknowledge."""

from __future__ import annotations

import pytest

from autoreviewer.web.commands import request_review
from autoreviewer.web.routing import PullRequestUpdated, ReviewRequested
from tests.review_fakes import FakeGitHub, GitRepo

pytestmark = pytest.mark.anyio

REQUEST = ReviewRequested(installation_id=1, owner="o", repo="r", number=7, comment_id=99)


class Queue:
    def __init__(self, *, done: bool = False) -> None:
        self.queued: list[PullRequestUpdated] = []
        self.done = done

    async def __call__(self, pull: PullRequestUpdated) -> bool:
        self.queued.append(pull)
        return self.done


async def test_a_request_queues_the_current_head_and_says_on_it(repo: GitRepo) -> None:
    github, queue = FakeGitHub(repo), Queue()
    pull = await request_review(github, REQUEST, queue)
    assert queue.queued == [pull] == [PullRequestUpdated(1, "o", "r", 7, repo.head)]
    assert github.reactions == [(99, "eyes")]


async def test_an_already_reviewed_head_gets_a_thumbs_up(repo: GitRepo) -> None:
    github = FakeGitHub(repo)
    await request_review(github, REQUEST, Queue(done=True))
    assert github.reactions == [(99, "+1")]


async def test_a_closed_pr_is_left_alone(repo: GitRepo) -> None:
    github, queue = FakeGitHub(repo, pr_state="closed"), Queue()
    assert await request_review(github, REQUEST, queue) is None
    assert queue.queued == [] and github.reactions == []


async def test_a_failed_reaction_still_queues_the_review(repo: GitRepo) -> None:
    github, queue = FakeGitHub(repo, fail_reactions=True), Queue()
    assert await request_review(github, REQUEST, queue) is not None
    assert len(queue.queued) == 1
