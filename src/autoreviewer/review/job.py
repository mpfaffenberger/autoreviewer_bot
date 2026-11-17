"""The three phases of reviewing one PR push. `durable.py` runs each as a DBOS step.

    start_check  ->  review_and_post  ->  finish_check

Each phase is safe to replay after a crash: the check run is created once (its step's result is
checkpointed), and posting skips a commit the bot already reviewed.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic_ai import Agent

from autoreviewer.github import (
    CommitNotFound,
    Conclusion,
    PullRequestReader,
    PullRequests,
    PullRequestState,
    Repo,
    ReviewComment,
    ReviewRejected,
)
from autoreviewer.review.diff import MAX_CHANGED_LINES, select
from autoreviewer.review.lint import LintError, type_findings
from autoreviewer.review.memory import PriorReview, ReviewMemory, conversation_id
from autoreviewer.review.pipeline import BLOCK_AT_PRIORITY, Continuation, Outcome, PullRequestText, review
from autoreviewer.review.progress import Progress
from autoreviewer.review.render import comment_body, review_body
from autoreviewer.review.schemas import LensReport, Verification
from autoreviewer.review.workspace import RepoSource, Workspace, read_changes, read_changes_since_previous
from autoreviewer.web.routing import PullRequestUpdated

logger = logging.getLogger("autoreviewer.job")

PROGRESS_INTERVAL_S = 30.0
"""How often a running review updates its check run, at most (GitHub rate-limits App writes)."""

AgentFactory = Callable[[Workspace], tuple[Agent[None, LensReport], Agent[None, Verification]]]


@dataclass(frozen=True)
class ReviewDeps:
    github: PullRequests
    source: RepoSource
    """Where the code comes from: a git checkout of the PR head plus its merge-base."""
    agents: AgentFactory
    """Build (lens, verifier) over a checkout."""
    work_dir: Path
    bot_login: str
    """Our own GitHub login, e.g. chaos-emerald-reviewer[bot]; used to avoid reviewing a commit twice."""
    models_note: str
    """Shown in the review footer, e.g. "correctness by qwen3.8-flash-next, verified by deepseek-v4.1-flash"."""
    memory: ReviewMemory | None = None
    """Where each PR's lens conversation is kept, so a new commit continues the last review."""
    lens_version: str = ""
    """Fingerprint of the lens prompt and model (see agents.lens_version)."""
    block_at: int = BLOCK_AT_PRIORITY
    """Confirmed findings at this priority or more urgent block the merge (1: P0 and P1)."""


@dataclass(frozen=True)
class CheckResult:
    title: str
    summary: str
    conclusion: Conclusion
    details: str = ""
    """What the review did (see progress.py), kept on the finished check run."""


@dataclass(frozen=True)
class Reviewed:
    outcome: Outcome
    remember: PriorReview | None
    """What to save for the next review of this PR; None without memory."""


class Superseded(Exception):
    """The PR moved on (new push, closed) while we worked; the newer event gets its own review."""


def repo_of(pull: PullRequestUpdated) -> Repo:
    return Repo(pull.installation_id, pull.owner, pull.repo)


async def start_check(deps: ReviewDeps, pull: PullRequestUpdated) -> int:
    return await deps.github.start_check(repo_of(pull), pull.head_sha)


async def finish_check(deps: ReviewDeps, pull: PullRequestUpdated, check_id: int, result: CheckResult) -> None:
    await deps.github.finish_check(
        repo_of(pull),
        check_id,
        conclusion=result.conclusion,
        title=result.title,
        summary=result.summary,
        text=result.details,
    )


async def review_and_post(deps: ReviewDeps, pull: PullRequestUpdated, check_id: int | None = None) -> CheckResult:
    """Never raises: every outcome, including a crash, becomes the check run's result.

    With `check_id`, the check run shows what the review is doing while it runs (see progress.py).
    """
    progress = Progress()
    publisher = asyncio.create_task(_publish(deps, repo_of(pull), check_id, progress)) if check_id else None
    try:
        result = await _guarded_review(deps, pull, progress)
    finally:
        if publisher is not None:
            publisher.cancel()
    return replace(result, details=progress.details())


async def _guarded_review(deps: ReviewDeps, pull: PullRequestUpdated, progress: Progress) -> CheckResult:
    try:
        return await _review(deps, repo_of(pull), pull, progress)
    except Superseded:
        return CheckResult("Superseded", "A newer push replaced this commit before the review finished.", "neutral")
    except Exception:
        logger.exception("reviewing %s/%s#%d at %s failed", pull.owner, pull.repo, pull.number, pull.head_sha[:7])
        return CheckResult(
            "Review failed",
            "The review could not be completed. Push again to retry; the bot's logs have the details.",
            "neutral",
        )


async def _publish(deps: ReviewDeps, repo: Repo, check_id: int, progress: Progress) -> None:
    """Show progress on the check run every PROGRESS_INTERVAL_S while something changed. Best-effort: GitHub
    limits how often an App may write, and a failed update must never touch the review."""
    published = progress.version
    while True:
        await asyncio.sleep(PROGRESS_INTERVAL_S)
        if progress.version == published:
            continue
        published = progress.version
        try:
            await deps.github.update_check(
                repo, check_id, title=progress.phase, summary=progress.summary(), text=progress.details()
            )
        except Exception:
            logger.warning("could not show progress on check run %d", check_id, exc_info=True)


async def review_change(
    *,
    reader: PullRequestReader,
    source: RepoSource,
    agents: AgentFactory,
    work_dir: Path,
    repo: Repo,
    number: int,
    state: PullRequestState,
    head_sha: str,
    memory: ReviewMemory | None = None,
    lens_version: str = "",
    block_at: int = BLOCK_AT_PRIORITY,
    progress: Progress | None = None,
) -> Reviewed | CheckResult:
    """Review one PR head: check it out, find what changed, run lens and verifier.

    With `memory`, a head that adds commits on top of the last reviewed one continues that review's
    conversation; anything else (first review, force-push, moved merge-base, changed prompt or model)
    starts fresh. Returns the findings (plus what to remember, which the caller saves only once it knows
    the PR is still open), or a CheckResult when there is nothing to review or it is too large. Shared
    by the bot's job (which then posts) and on-demand reviews (which print).
    """
    base = (await reader.compare(repo, state.base_sha, head_sha)).merge_base
    conversation = conversation_id(repo, number)
    prior = await _resumable(memory, reader, repo, conversation, base=base, head_sha=head_sha, version=lens_version)

    with tempfile.TemporaryDirectory(prefix="review-", dir=work_dir) as scratch:
        workspace = await source.checkout(
            repo, head=head_sha, base=base, dest=Path(scratch) / "repo", previous=prior.head_sha if prior else None
        )
        changes, patches = await read_changes(workspace)
        selection = select(changes)
        if not selection.reviewable:
            return CheckResult(
                "Nothing to review", "Only removed, generated, vendored, or binary files changed.", "success"
            )
        if selection.changed_lines > MAX_CHANGED_LINES:
            return CheckResult(
                "Too large to review",
                f"{selection.changed_lines} changed lines; the limit is {MAX_CHANGED_LINES}.",
                "neutral",
            )
        continuation = None
        if prior is not None and memory is not None:
            history = await memory.history(conversation, prior)
            if history:
                changed_since = await read_changes_since_previous(workspace)
                continuation = Continuation(prior, history, head_sha, changed_since)
        persistence, run_id = memory.persistence(conversation) if memory else (None, "")
        try:
            typed = await type_findings(workspace.root, selection.reviewable, patches)
        except LintError:  # a broken linter costs the type checks, never the review
            logger.exception("%s at %s: type checks skipped", conversation, head_sha[:7])
            typed = []
        lens, verifier = agents(workspace)
        pr = PullRequestText(state.title, state.body, selection.reviewable, selection.skipped, patches)
        outcome = await review(
            pr,
            lens,
            verifier,
            continuation=continuation,
            lens_capabilities=[persistence] if persistence else (),
            conversation=conversation,
            block_at=block_at,
            checked=typed,
            progress=progress,
        )
    logger.info(
        "%s at %s: %s", conversation, head_sha[:7], "continued the last review" if continuation else "fresh review"
    )
    remember = _prior_review(run_id, head_sha, base, lens_version, outcome) if memory is not None else None
    return Reviewed(outcome, remember)


async def _resumable(
    memory: ReviewMemory | None,
    reader: PullRequestReader,
    repo: Repo,
    conversation: str,
    *,
    base: str,
    head_sha: str,
    version: str,
) -> PriorReview | None:
    """The last review, if this head simply adds commits on top of what it saw. Otherwise None."""
    if memory is None or (prior := memory.last(conversation)) is None:
        return None
    if prior.version != version or prior.base_sha != base or prior.head_sha == head_sha:
        return None  # another prompt/model, a moved merge-base, or the same head again: start fresh
    try:
        comparison = await reader.compare(repo, prior.head_sha, head_sha)
    except CommitNotFound:
        return None  # the reviewed head is gone (force-pushed away, then garbage-collected)
    if comparison.status != "ahead":
        return None  # force-push, rebase, or reset: the old conversation describes code that's gone
    return prior


def _prior_review(run_id: str, head_sha: str, base: str, version: str, outcome: Outcome) -> PriorReview:
    return PriorReview(
        run_id=run_id,
        head_sha=head_sha,
        base_sha=base,
        version=version,
        # Only what the lens said: linter findings are recomputed every review, and aren't its to re-check.
        findings=[
            {"path": f.path, "line": f.line, "title": f"[P{f.priority}] {f.title}"}
            for f in outcome.posted
            if f.kind == "correctness"
        ],
        dropped=[
            {"path": f.path, "line": f.line, "title": f.title, "reason": reason} for f, reason in outcome.rejected
        ],
        # Never as "dropped": a verifier failure is no verdict, and the next review must look again.
        unverified=[{"path": f.path, "line": f.line, "title": f.title} for f, _ in outcome.unverified],
    )


async def _review(deps: ReviewDeps, repo: Repo, pull: PullRequestUpdated, progress: Progress) -> CheckResult:
    state = await _current(deps, repo, pull)
    conversation = conversation_id(repo, pull.number)
    try:
        result = await review_change(
            reader=deps.github,
            source=deps.source,
            agents=deps.agents,
            work_dir=deps.work_dir,
            repo=repo,
            number=pull.number,
            state=state,
            head_sha=pull.head_sha,
            memory=deps.memory,
            lens_version=deps.lens_version,
            block_at=deps.block_at,
            progress=progress,
        )
    except Exception:
        await _forget_if_closed(deps, repo, pull, conversation)  # snapshots written mid-run must not outlive a close
        raise
    if isinstance(result, CheckResult):
        return result

    # The review took minutes. Forgetting wins: a PR closed meanwhile keeps nothing (its webhook already
    # deleted the memory, and this review must not write it back). A newer head keeps this analysis, which
    # the next review can continue from, but gets no post: it has its own review coming.
    latest = await deps.github.get_pull(repo, pull.number)
    if latest.state != "open":
        if deps.memory is not None:
            deps.memory.forget(conversation)
        raise Superseded
    if deps.memory is not None and result.remember is not None:
        deps.memory.remember(conversation, result.remember)
    if latest.head_sha != pull.head_sha:
        raise Superseded
    await _post(deps, repo, pull, result.outcome)
    return _summary(result.outcome)


async def _forget_if_closed(deps: ReviewDeps, repo: Repo, pull: PullRequestUpdated, conversation: str) -> None:
    if deps.memory is None:
        return
    try:
        if (await deps.github.get_pull(repo, pull.number)).state != "open":
            deps.memory.forget(conversation)
    except Exception:  # never let cleanup hide the original failure
        logger.exception("could not check whether %s was closed", conversation)


async def _current(deps: ReviewDeps, repo: Repo, pull: PullRequestUpdated) -> PullRequestState:
    state = await deps.github.get_pull(repo, pull.number)
    if state.head_sha != pull.head_sha or state.state != "open":
        raise Superseded
    return state


async def _post(deps: ReviewDeps, repo: Repo, pull: PullRequestUpdated, outcome: Outcome) -> None:
    if await deps.github.has_review(repo, pull.number, commit_sha=pull.head_sha, author=deps.bot_login):
        # A replay after a crash between posting and checkpointing: the review is already there.
        logger.info(
            "%s/%s#%d at %s already reviewed; not posting again", pull.owner, pull.repo, pull.number, pull.head_sha[:7]
        )
        return
    footer = f"<sub>autoreviewer · {deps.models_note} · `{pull.head_sha[:7]}`</sub>"
    # The verdict is computed, never generated: a confirmed finding at the blocking priority blocks the
    # merge; advisory findings are posted as comments on an approval.
    event = "REQUEST_CHANGES" if outcome.blocking else "APPROVE"
    try:
        await deps.github.create_review(
            repo,
            pull.number,
            commit_sha=pull.head_sha,
            event=event,
            body=review_body(outcome, footer=footer),
            comments=[
                ReviewComment(f.path, f.line, comment_body(f, blocking=outcome.is_blocking(f))) for f in outcome.inline
            ],
        )
    except ReviewRejected:
        # GitHub's diff view can differ from ours (e.g. files it doesn't render). Keep every finding,
        # just not inline: same verdict, findings listed in the review body.
        logger.warning(
            "inline comments rejected for %s/%s#%d; posting findings in the body", pull.owner, pull.repo, pull.number
        )
        await deps.github.create_review(
            repo,
            pull.number,
            commit_sha=pull.head_sha,
            event=event,
            body=review_body(replace(outcome, inline=[], general=outcome.posted), footer=footer),
            comments=[],
        )


def _summary(outcome: Outcome) -> CheckResult:
    notes = ""
    advisory = [f for f in outcome.posted if not outcome.is_blocking(f)]
    if advisory:
        notes += f" {len(advisory)} advisory (non-blocking) finding(s) posted."
    if outcome.rejected:
        notes += f" {len(outcome.rejected)} candidate finding(s) were dropped by verification."
    if outcome.unverified:
        notes += f" {len(outcome.unverified)} candidate finding(s) could not be verified and will be checked again."
    if outcome.blocking:
        count = len(outcome.posted) - len(advisory)
        return CheckResult(
            f"{count} blocking issue(s)", f"Changes requested; see the review comments.{notes}", "failure"
        )
    return CheckResult("No blocking issues", f"Approved.{notes}", "success")
