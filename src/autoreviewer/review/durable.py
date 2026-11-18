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

import logging
import sqlite3
from pathlib import Path

from dbos import DBOS, SetWorkflowID, WorkflowHandleAsync, WorkflowStatusString

from autoreviewer.review import job
from autoreviewer.web.routing import PullRequestUpdated

QUEUE = "reviews"

APPLICATION_VERSION = "review-workflow-v1"
"""Pinned so in-flight reviews survive a redeploy (DBOS otherwise only recovers workflows of the same
code hash). Bump it whenever the workflow's sequence of steps changes."""

MIN_SQLITE = (3, 42)
"""DBOS's SQLite schema defaults timestamps to unixepoch('subsec'), which older SQLite evaluates to NULL,
failing with an obscure NOT NULL error at launch (hit on Debian bookworm's 3.40)."""

MAX_RECOVERY_ATTEMPTS = 3
"""A review that keeps crashing the process is parked after this many recoveries instead of looping on the GPU."""

_deps: job.ReviewDeps | None = None


def _require_deps() -> job.ReviewDeps:
    if _deps is None:
        raise RuntimeError("durable.launch() must run before reviews are executed")
    return _deps


logger = logging.getLogger("autoreviewer.durable")


def workflow_id(pull: PullRequestUpdated) -> str:
    """One review per PR head: redeliveries and duplicate events can never start a second one."""
    return f"review:{pull.owner}/{pull.repo}#{pull.number}@{pull.head_sha}".lower()


@DBOS.step(retries_allowed=True, max_attempts=3)
async def _start_check(pull: PullRequestUpdated) -> int:
    return await job.start_check(_require_deps(), pull)


@DBOS.step()  # no retries: review_and_post never raises, and model time isn't free
async def _review_and_post(pull: PullRequestUpdated, check_id: int) -> job.CheckResult:
    return await job.review_and_post(_require_deps(), pull, check_id)


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


async def launch(deps: job.ReviewDeps, *, concurrency: int) -> None:
    """Start DBOS (recovering interrupted reviews) and register the review queue."""
    global _deps
    _deps = deps
    DBOS.launch()
    await DBOS.register_queue_async(QUEUE, worker_concurrency=concurrency)


async def enqueue(pull: PullRequestUpdated) -> WorkflowHandleAsync[job.CheckResult]:
    queue = await DBOS.retrieve_queue_async(QUEUE)
    if queue is None:
        raise RuntimeError("the review queue is not registered; call durable.launch() first")
    with SetWorkflowID(workflow_id(pull)):
        return await queue.enqueue_async(review_workflow, pull)


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
    if retry_failed and status in _ENDED_WITHOUT_RESULT:
        await _retry(handle.workflow_id, status)
        return False
    if (
        retry_failed
        and status == WorkflowStatusString.SUCCESS.value
        and (await handle.get_result()).conclusion == "neutral"
    ):
        # Finished without a verdict (partially reviewed, timed out, failed): what "push again or ask again" promises.
        logger.info("re-running %s, which ended without a verdict", handle.workflow_id)
        await DBOS.fork_workflow_async(handle.workflow_id, REVIEW_STEP, queue_name=QUEUE)
        return False
    return status == WorkflowStatusString.SUCCESS.value


async def _retry(workflow_id: str, status: str) -> None:
    """Run an ended review again from its first failed step. Resuming would replay a recorded failure, so a
    failed step means a fork (a new workflow keeping the steps before it, e.g. the check run); with no
    failure recorded (cancelled, or crashed mid-step) it resumes where it stopped."""
    steps = await DBOS.list_workflow_steps_async(workflow_id)
    failed = [step["function_id"] for step in steps if step["error"] is not None]
    if failed:
        logger.info("retrying the %s review %s from step %d", status.lower(), workflow_id, min(failed))
        await DBOS.fork_workflow_async(workflow_id, min(failed), queue_name=QUEUE)
    else:
        logger.info("resuming the %s review %s", status.lower(), workflow_id)
        # On the review queue: by default DBOS resumes on its internal queue, which ignores our concurrency.
        await DBOS.resume_workflow_async(workflow_id, queue_name=QUEUE)


def shutdown() -> None:
    # In-flight reviews are not awaited: they are recovered by the next process.
    DBOS.destroy()
