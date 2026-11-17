"""Answer `@<bot> review` comments: queue the same review a push would, and say so with a reaction."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from autoreviewer.github import Commands, Reaction, Repo
from autoreviewer.web.routing import PullRequestUpdated, ReviewRequested

logger = logging.getLogger("autoreviewer.commands")

Enqueue = Callable[[PullRequestUpdated], Awaitable[bool]]
"""Queue a review of one head; True if that head's review had already finished (nothing new runs)."""


async def request_review(github: Commands, request: ReviewRequested, enqueue: Enqueue) -> PullRequestUpdated | None:
    """Queue a review of the PR's current head; None if the PR isn't open.

    It is the push path's review, with the same durable workflow ID, so asking twice about one commit
    costs nothing. Drafts are reviewed too: the request is explicit.
    """
    repo = Repo(request.installation_id, request.owner, request.repo)
    state = await github.get_pull(repo, request.number)
    if state.state != "open":
        logger.info("%s/%s#%d is %s; not reviewing", request.owner, request.repo, request.number, state.state)
        return None
    pull = PullRequestUpdated(request.installation_id, request.owner, request.repo, request.number, state.head_sha)
    done = await enqueue(pull)
    # 👀 "on it" or 👍 "this commit already has its review". Cosmetic: a failure must not fail the delivery.
    reaction: Reaction = "+1" if done else "eyes"
    try:
        await github.react(repo, request.comment_id, reaction)
    except Exception:
        logger.exception("could not react to comment %d on %s/%s", request.comment_id, request.owner, request.repo)
    return pull
