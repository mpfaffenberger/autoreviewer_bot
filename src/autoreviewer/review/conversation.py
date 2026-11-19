"""Answer replies under the bot's findings: one answer per comment, re-checked against the code.

The thread on GitHub is the conversation's state: who said what, and what the bot already answered (each answer
ends with a marker naming the comment it answers). A reply is data, never an instruction: the conversation agent
withdraws a finding only when the code at the PR's head shows it's wrong, and the marker it leaves is what any
later verdict reads. Loops are bounded: never answering itself, one answer per comment, and at most
MAX_ANSWERS per thread, which also ends a ping-pong between two bots.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic_ai import Agent

from autoreviewer.github import Conversations, Repo, ThreadComment
from autoreviewer.review.agents import CONVERSATION_LIMITS
from autoreviewer.review.posting import answer_markers, answered, finding_marker, reconsider
from autoreviewer.review.render import sanitize
from autoreviewer.review.schemas import ThreadAnswer
from autoreviewer.review.workspace import RepoSource, Workspace
from autoreviewer.web.routing import ThreadReply

logger = logging.getLogger("autoreviewer.conversation")

MAX_ANSWERS = 5
"""The bot's answers per thread. Past this, a person should settle it; it also ends any bot-to-bot loop."""
MAX_QUOTED = 4_000
"""Characters of each thread comment shown to the agent: a reply can't flood its context."""

ConversationAgents = Callable[[Workspace], Agent[None, ThreadAnswer]]

Outcome = Literal["answered", "withdrawn", "not our thread", "already answered", "answer limit", "pr not open"]


@dataclass(frozen=True)
class ConversationDeps:
    github: Conversations
    source: RepoSource
    agents: ConversationAgents
    work_dir: Path
    bot_login: str
    block_at: int


async def answer(deps: ConversationDeps, reply: ThreadReply) -> Outcome:
    """Answer `reply` once, if it's under one of the bot's findings. A re-run (say, after a crash) finds an answer
    already posted by its marker, so it doesn't answer twice; two runs for one comment that overlap could, which
    the durable workflow's ID (one per comment) rules out."""
    repo = _repo(reply)
    thread = await deps.github.thread(repo, reply.number, reply.root_id)
    if not thread or thread[0].author != deps.bot_login or finding_marker(thread[0].body) is None:
        return "not our thread"
    answers = [comment for comment in thread[1:] if comment.author == deps.bot_login]
    if any((done := answered(comment.body)) and done[0] == reply.comment_id for comment in answers):
        return "already answered"
    if len(answers) >= MAX_ANSWERS:
        logger.info("%s/%s#%d thread %d: %d answers already", reply.owner, reply.repo, reply.number, reply.root_id,
                    len(answers))  # fmt: skip
        return "answer limit"
    state = await deps.github.get_pull(repo, reply.number)
    if state.state != "open":
        return "pr not open"
    base = (await deps.github.compare(repo, state.base_sha, state.head_sha)).merge_base
    with tempfile.TemporaryDirectory(prefix="answer-", dir=deps.work_dir) as scratch:
        workspace = await deps.source.checkout(repo, head=state.head_sha, base=base, dest=Path(scratch) / "repo")
        result = await deps.agents(workspace).run(
            _prompt(state.title, thread, reply.comment_id), usage_limits=CONVERSATION_LIMITS
        )
    verdict = result.output
    withdrawn = verdict.finding == "withdrawn"
    logger.info("%s/%s#%d thread %d: finding %s (%s)", reply.owner, reply.repo, reply.number, reply.root_id,
                verdict.finding, verdict.reason)  # fmt: skip
    body = f"{sanitize(verdict.reply)}\n\n{answer_markers(reply.comment_id, withdrawn=withdrawn)}"
    await deps.github.reply(repo, reply.number, reply.root_id, body)
    return "withdrawn" if withdrawn else "answered"


def _prompt(title: str, thread: list[ThreadComment], comment_id: int) -> str:
    def quoted(comment: ThreadComment) -> str:
        text = comment.body if len(comment.body) <= MAX_QUOTED else comment.body[:MAX_QUOTED] + " [...]"
        return f"<comment id={comment.id} author={comment.author!r}>\n{text}\n</comment>"

    root, replies = thread[0], thread[1:]
    return "\n\n".join(
        [
            f"Pull request: {title!r}",
            f"Your finding, as posted:\n{quoted(root)}",
            "The thread since:\n" + ("\n".join(map(quoted, replies)) if replies else "(nothing yet)"),
            f"Answer comment {comment_id}.",
        ]
    )


async def reconsider_verdict(deps: ConversationDeps, reply: ThreadReply) -> bool:
    """After a withdrawal: approve the PR's head if nothing blocking is left standing (posting.reconsider)."""
    return await reconsider(deps.github, _repo(reply), reply.number, author=deps.bot_login, block_at=deps.block_at)


def _repo(reply: ThreadReply) -> Repo:
    return Repo(reply.installation_id, reply.owner, reply.repo)
