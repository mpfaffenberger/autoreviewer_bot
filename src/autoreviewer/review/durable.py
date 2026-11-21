"""Durable reviews: one DBOS workflow per review attempt, run from a queue, recovered after a restart.

Verified against DBOS 3.5 before writing this: a duplicate workflow ID returns the existing workflow,
async workflows run on the caller's event loop, and after a hard crash a new process skips completed
steps and re-runs only the interrupted one. Queues must be registered (and retrieved) after launch,
with the `_async` calls from async code.

The steps are the three phases in `job.py`. The model calls inside `review_and_post` are not
individually durable: an interrupted review restarts that step from the top, which costs about a
minute of GPU time and nothing else.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Coroutine
from pathlib import Path
from time import time
from typing import Any

from dbos import DBOS, SetWorkflowID, WorkflowHandleAsync, WorkflowStatusString

from autoreviewer.review import conversation, job
from autoreviewer.web.routing import PullRequestUpdated, ThreadReply

QUEUE = "reviews"
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


def workflow_id(pull: PullRequestUpdated) -> str:
    """Stable first attempt ID, shared by a push and a first explicit request."""
    return f"review:{pull.owner}/{pull.repo}#{pull.number}@{pull.head_sha}".lower()


@DBOS.step(retries_allowed=True, max_attempts=3)
async def _start_check(pull: PullRequestUpdated) -> int:
    return await job.start_check(_require_deps(), pull)


@DBOS.step()  # no retries: review_and_post never raises, and model time isn't free
async def _review_and_post(pull: PullRequestUpdated, check_id: int) -> job.CheckResult:
    # DBOS already timestamped step 1, after dequeue. Read its checkpoint *inside* step 2:
    # no extra workflow operation on replay, and even old integer check IDs need no migration.
    run_id = DBOS.workflow_id
    assert run_id is not None
    steps = await DBOS.list_workflow_steps_async(run_id, limit=1, load_output=False)
    if not steps or steps[0]["function_id"] != 1 or (started_ms := steps[0]["started_at_epoch_ms"]) is None:
        raise RuntimeError(f"Missing durable check start for {run_id}")
    elapsed_s = max(0.0, time() - started_ms / 1000)
    deps = _require_deps()
    budget_s = min(deps.hard_deadline_s, job.MAX_REVIEW_S)
    return await job.review_and_post(deps, pull, check_id, remaining_s=max(0.0, budget_s - elapsed_s))


@DBOS.step(retries_allowed=True, max_attempts=3)
async def _finish_check(pull: PullRequestUpdated, check_id: int, result: job.CheckResult) -> None:
    await job.finish_check(_require_deps(), pull, check_id, result)


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
    deps: job.ReviewDeps, conversation_deps: conversation.ConversationDeps | None = None, *, concurrency: int
) -> None:
    """Start DBOS (recovering interrupted reviews and answers) and register the queues."""
    global _deps, _conversation_deps
    _deps, _conversation_deps = deps, conversation_deps
    DBOS.launch()
    await DBOS.register_queue_async(QUEUE, worker_concurrency=concurrency)
    await DBOS.register_queue_async(ANSWER_QUEUE, worker_concurrency=ANSWER_CONCURRENCY)


async def enqueue(pull: PullRequestUpdated) -> WorkflowHandleAsync[job.CheckResult]:
    return await _enqueue(QUEUE, workflow_id(pull), review_workflow, pull)


async def _enqueue[Arg, Result](
    queue_name: str, workflow_id: str, workflow: Callable[[Arg], Coroutine[Any, Any, Result]], arg: Arg
) -> WorkflowHandleAsync[Result]:
    """Run `workflow(arg)` on a registered queue under `workflow_id`: a second enqueue of an ID joins the first."""
    queue = await DBOS.retrieve_queue_async(queue_name)
    if queue is None:
        raise RuntimeError(f"the {queue_name} queue is not registered; call durable.launch() first")
    with SetWorkflowID(workflow_id):
        return await queue.enqueue_async(workflow, arg)


_RUNNING = {"ENQUEUED", "PENDING", "DELAYED", "SUSPENDED"}


async def _select_attempt(pull: PullRequestUpdated, *, explicit: bool) -> str:
    base = workflow_id(pull)
    attempt = base
    previous = None
    number = 1
    while True:
        status = await DBOS.get_workflow_status_async(attempt)
        if status is None:
            if previous is not None and not explicit:
                return previous  # pushes never allocate another attempt
            # Concurrent selectors finding the same empty slot enqueue the same DBOS ID.
            return (await _enqueue(QUEUE, attempt, review_workflow, pull)).workflow_id
        if status.status in _RUNNING:
            return attempt
        if number == 1:
            # A retry queued by the previous version may still be running at deploy time.
            forks = await DBOS.list_workflows_async(
                forked_from=base, status=sorted(_RUNNING), limit=1, load_input=False, load_output=False
            )
            if forks:
                return forks[0].workflow_id
        number += 1
        previous, attempt = attempt, f"{base}:attempt:{number}"


@DBOS.workflow()
async def _requested_attempt(pull: PullRequestUpdated) -> str:
    # DBOS checkpoints both the selection reads and child enqueue. A redelivery returns
    # the same attempt even when it originally joined a push, or the PR head has moved.
    return await _select_attempt(pull, explicit=True)


async def enqueue_review(pull: PullRequestUpdated, comment_id: int | None = None) -> bool:
    """Queue or join an attempt; True if the selected workflow is already successful."""
    if comment_id is None:
        attempt = await _select_attempt(pull, explicit=False)
    else:
        with SetWorkflowID(f"review-request:{pull.owner}/{pull.repo}#{pull.number}:{comment_id}".lower()):
            receipt = await DBOS.start_workflow_async(_requested_attempt, pull)
        attempt = await receipt.get_result()  # waits for dispatch, not the review
    status = await DBOS.get_workflow_status_async(attempt)
    return status is not None and status.status == WorkflowStatusString.SUCCESS.value


def shutdown() -> None:
    # In-flight reviews are not awaited: they are recovered by the next process.
    DBOS.destroy()
