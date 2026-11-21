"""Durable reviews: one DBOS workflow per PR head, run from a queue, recovered after a restart.

Verified against DBOS 3.5 before writing this: a duplicate workflow ID returns the existing workflow,
async workflows run on the caller's event loop, and after a hard crash a new process skips completed
steps and re-runs only the interrupted one. Queues must be registered (and retrieved) after launch,
with the `_async` calls from async code.

The steps are the three phases in `job.py`. The model calls inside `review_and_post` are not
individually durable: an interrupted review restarts that step from the top, which costs about a
minute of GPU time and nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dbos import DBOS, SetWorkflowID, WorkflowHandleAsync, WorkflowStatusString

from autoreviewer.review import conversation, job
from autoreviewer.web.routing import PullRequestUpdated, ThreadReply

QUEUE = "reviews"
LOCAL_QUEUE = "reviews-local"
ANSWER_QUEUE = "answers"
"""Answers to replies run beside reviews, never behind one: a reply shouldn't wait for a 15-minute review."""
ANSWER_CONCURRENCY = 2

APPLICATION_VERSION = "review-workflow-v1"
"""Pinned so in-flight reviews survive a redeploy (DBOS otherwise only recovers workflows of the same
code hash). Bump it whenever the workflow's sequence of steps changes."""

MIN_SQLITE = (3, 42)
"""DBOS's SQLite schema defaults timestamps to unixepoch('subsec'), which older SQLite evaluates to NULL,
failing with an obscure NOT NULL error at launch (hit on Debian bookworm's 3.40)."""

MAX_RECOVERY_ATTEMPTS = 3
"""A review that keeps crashing the process is parked after this many recoveries instead of looping on the GPU."""


class _ReviewSlots:
    """Per worker, not per queue: either queue may contain work admitted under an older allowlist."""

    def __init__(self, concurrency: int, overflow: job.ReviewDeps | None, repos: frozenset[str]) -> None:
        if concurrency < 1:
            raise ValueError("review concurrency must be positive")
        self.primary_free = concurrency
        self.overflow = overflow
        self.repos = repos
        self.changed = asyncio.Condition()

    def eligible(self, pull: PullRequestUpdated) -> bool:
        return self.overflow is not None and ("*" in self.repos or f"{pull.owner}/{pull.repo}".lower() in self.repos)

    @asynccontextmanager
    async def claim(self, pull: PullRequestUpdated) -> AsyncIterator[job.ReviewDeps]:
        eligible = self.eligible(pull)
        async with self.changed:
            await self.changed.wait_for(lambda: self.primary_free > 0 or eligible)
            primary = self.primary_free > 0
            if primary:
                deps = _require_deps()
                self.primary_free -= 1
            else:
                assert self.overflow is not None
                deps = self.overflow
        try:
            yield deps
        finally:
            if primary:
                async with self.changed:
                    self.primary_free += 1
                    self.changed.notify_all()


_slots: _ReviewSlots | None = None
_deps: job.ReviewDeps | None = None
_conversation_deps: conversation.ConversationDeps | None = None


def _require_deps() -> job.ReviewDeps:
    if _deps is None:
        raise RuntimeError("durable.launch() must run before reviews are executed")
    return _deps


def _require_conversation_deps() -> conversation.ConversationDeps:
    if _conversation_deps is None:
        raise RuntimeError("durable.launch() must run before answers are executed")
    return _conversation_deps


logger = logging.getLogger("autoreviewer.durable")


def workflow_id(pull: PullRequestUpdated) -> str:
    """One review per PR head: redeliveries and duplicate events can never start a second one."""
    return f"review:{pull.owner}/{pull.repo}#{pull.number}@{pull.head_sha}".lower()


@DBOS.step(retries_allowed=True, max_attempts=3)
async def _start_check(pull: PullRequestUpdated) -> int:
    return await job.start_check(_require_deps(), pull)


@DBOS.step()  # no retries: review_and_post never raises, and model time isn't free
async def _review_and_post(pull: PullRequestUpdated, check_id: int) -> job.CheckResult:
    if _slots is None:
        raise RuntimeError("durable.launch() must run before reviews are executed")
    async with _slots.claim(pull) as deps:
        return await job.review_and_post(deps, pull, check_id)


@DBOS.step(retries_allowed=True, max_attempts=3)
async def _finish_check(pull: PullRequestUpdated, check_id: int, result: job.CheckResult) -> None:
    await job.finish_check(_require_deps(), pull, check_id, result)


REVIEW_STEP = 2
"""review_workflow's second step (_review_and_post): a re-run starts there, keeping the check run."""


@DBOS.workflow(max_recovery_attempts=MAX_RECOVERY_ATTEMPTS)
async def review_workflow(pull: PullRequestUpdated) -> job.CheckResult:
    check_id = await _start_check(pull)
    result = await _review_and_post(pull, check_id)
    await _finish_check(pull, check_id, result)
    return result


def answer_id(reply: ThreadReply) -> str:
    """One durable workflow per comment: a redelivery or a duplicate event joins the run already there instead of
    starting another, and a second reply in the same thread gets its own."""
    return f"answer:{reply.owner}/{reply.repo}#{reply.number}:{reply.comment_id}".lower()


# Retried: GitHub or a model failing for a moment shouldn't lose the answer. An answer already posted is found by
# its marker, and a runaway one returns "gave up" instead of raising, so it isn't retried.
@DBOS.step(retries_allowed=True, max_attempts=3)
async def _answer(reply: ThreadReply) -> str:
    return await conversation.answer(_require_conversation_deps(), reply)


@DBOS.step(retries_allowed=True, max_attempts=3)  # GitHub calls only, and it re-reads everything each time
async def _reconsider(reply: ThreadReply) -> bool:
    return await conversation.reconsider_verdict(_require_conversation_deps(), reply)


@DBOS.workflow(max_recovery_attempts=MAX_RECOVERY_ATTEMPTS)
async def answer_workflow(reply: ThreadReply) -> str:
    outcome = await _answer(reply)
    if outcome == "withdrawn":
        await _reconsider(reply)
    return outcome


async def enqueue_answer(reply: ThreadReply) -> WorkflowHandleAsync[str]:
    return await _enqueue(ANSWER_QUEUE, answer_id(reply), answer_workflow, reply)


def configure(database: Path) -> None:
    """Create the DBOS instance (SQLite system database). Call once, outside the event loop."""
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is too old for DBOS; need {'.'.join(map(str, MIN_SQLITE))}+ "
            "(use a newer base image, e.g. Debian trixie)"
        )
    DBOS(
        config={
            "name": "autoreviewer",
            "system_database_url": f"sqlite:///{database}",
            "application_version": APPLICATION_VERSION,
            "log_level": "WARNING",
        }
    )


async def launch(
    deps: job.ReviewDeps,
    conversation_deps: conversation.ConversationDeps | None = None,
    *,
    concurrency: int,
    overflow_deps: job.ReviewDeps | None = None,
    overflow_repos: frozenset[str] = frozenset(),
) -> None:
    """Start DBOS (recovering interrupted reviews and answers) and register the queues."""
    global _deps, _conversation_deps, _slots
    _slots = _ReviewSlots(concurrency, overflow_deps, overflow_repos)
    _deps, _conversation_deps = deps, conversation_deps
    DBOS.launch()
    enabled = overflow_deps is not None and bool(overflow_repos)
    await DBOS.register_queue_async(QUEUE, worker_concurrency=None if enabled else concurrency)
    # Register even when disabled so any previously queued local work can drain.
    await DBOS.register_queue_async(
        LOCAL_QUEUE, worker_concurrency=None if enabled and "*" in overflow_repos else concurrency
    )
    await DBOS.register_queue_async(ANSWER_QUEUE, worker_concurrency=ANSWER_CONCURRENCY)


def _review_queue(pull: PullRequestUpdated) -> str:
    # Separate new local-only waiters so they cannot fill the eligible queue's admission window.
    # Old mixed backlogs can still head-of-line block; we never migrate or duplicate durable jobs.
    if _slots and _slots.overflow is not None and _slots.repos and not _slots.eligible(pull):
        return LOCAL_QUEUE
    return QUEUE


async def enqueue(pull: PullRequestUpdated) -> WorkflowHandleAsync[job.CheckResult]:
    return await _enqueue(_review_queue(pull), workflow_id(pull), review_workflow, pull)


async def _enqueue[Arg, Result](
    queue_name: str, workflow_id: str, workflow: Callable[[Arg], Coroutine[Any, Any, Result]], arg: Arg
) -> WorkflowHandleAsync[Result]:
    """Run `workflow(arg)` on a registered queue under `workflow_id`: a second enqueue of an ID joins the first."""
    queue = await DBOS.retrieve_queue_async(queue_name)
    if queue is None:
        raise RuntimeError(f"the {queue_name} queue is not registered; call durable.launch() first")
    with SetWorkflowID(workflow_id):
        return await queue.enqueue_async(workflow, arg)


_RUNNING = frozenset({WorkflowStatusString.PENDING.value, WorkflowStatusString.ENQUEUED.value})
_ENDED_WITHOUT_RESULT = frozenset(
    status.value
    for status in (
        WorkflowStatusString.CANCELLED,
        WorkflowStatusString.ERROR,
        WorkflowStatusString.MAX_RECOVERY_ATTEMPTS_EXCEEDED,
    )
)


async def enqueue_review(pull: PullRequestUpdated, *, retry_failed: bool = False) -> bool:
    """Queue a review of this head (a no-op if one exists); True if it had already finished with a verdict.

    With `retry_failed` (an explicit request, never a redelivered push), a review of this head that was
    cancelled, crashed, parked after repeated crashes, or finished without a verdict runs again.
    """
    handle = await enqueue(pull)
    status = (await handle.get_status()).status
    if not retry_failed:
        return status == WorkflowStatusString.SUCCESS.value
    # Retries are forks of this workflow: one at a time, and none once one has reached a verdict.
    forks = await DBOS.list_workflows_async(forked_from=handle.workflow_id)
    if any(fork.status in _RUNNING for fork in forks):
        return False  # already being retried
    if any(fork.status == WorkflowStatusString.SUCCESS.value and _has_verdict(fork.output) for fork in forks):
        return True
    if status in _ENDED_WITHOUT_RESULT:
        await _retry(handle.workflow_id, status, _review_queue(pull))
        return False
    if status == WorkflowStatusString.SUCCESS.value and not _has_verdict(await handle.get_result()):
        # Finished without a verdict (partially reviewed, timed out, failed): what "push again or ask again" promises.
        logger.info("re-running %s, which ended without a verdict", handle.workflow_id)
        await DBOS.fork_workflow_async(handle.workflow_id, REVIEW_STEP, queue_name=_review_queue(pull))
        return False
    return status == WorkflowStatusString.SUCCESS.value


def _has_verdict(result: object) -> bool:
    """A finished review that approved or requested changes (not partial, timed out, failed, or superseded)."""
    return getattr(result, "conclusion", "neutral") != "neutral"


async def _retry(workflow_id: str, status: str, queue: str) -> None:
    """Run an ended review again from its first failed step. Resuming would replay a recorded failure, so a
    failed step means a fork (a new workflow keeping the steps before it, e.g. the check run); with no
    failure recorded (cancelled, or crashed mid-step) it resumes where it stopped."""
    steps = await DBOS.list_workflow_steps_async(workflow_id)
    failed = [step["function_id"] for step in steps if step["error"] is not None]
    if failed:
        logger.info("retrying the %s review %s from step %d", status.lower(), workflow_id, min(failed))
        await DBOS.fork_workflow_async(workflow_id, min(failed), queue_name=queue)
    else:
        logger.info("resuming the %s review %s", status.lower(), workflow_id)
        # On the review queue: by default DBOS resumes on its internal queue, which ignores our concurrency.
        await DBOS.resume_workflow_async(workflow_id, queue_name=queue)


def shutdown() -> None:
    # In-flight reviews are not awaited: they are recovered by the next process.
    DBOS.destroy()
