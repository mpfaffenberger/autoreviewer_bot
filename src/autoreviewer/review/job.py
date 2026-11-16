"""The three phases of reviewing one PR push. `durable.py` runs each as a DBOS step.

    start_check  ->  review_and_post  ->  finish_check

Each phase is safe to replay after a crash: the check run is created once (its step's result is
checkpointed), and posting skips a commit the bot already reviewed.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import Agent

from autoreviewer.github import Conclusion, PullRequests, PullRequestState, Repo, ReviewComment, ReviewRejected
from autoreviewer.review.diff import MAX_CHANGED_LINES, select
from autoreviewer.review.pipeline import Outcome, PullRequestText, review
from autoreviewer.review.render import comment_body, review_body
from autoreviewer.review.schemas import LensReport, Verification
from autoreviewer.review.workspace import RepoSource, Workspace, read_changes
from autoreviewer.web.routing import PullRequestUpdated

logger = logging.getLogger("autoreviewer.job")

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


@dataclass(frozen=True)
class CheckResult:
    title: str
    summary: str
    conclusion: Conclusion


class Superseded(Exception):
    """The PR moved on (new push, closed) while we worked; the newer event gets its own review."""


def repo_of(pull: PullRequestUpdated) -> Repo:
    return Repo(pull.installation_id, pull.owner, pull.repo)


async def start_check(deps: ReviewDeps, pull: PullRequestUpdated) -> int:
    return await deps.github.start_check(repo_of(pull), pull.head_sha)


async def finish_check(deps: ReviewDeps, pull: PullRequestUpdated, check_id: int, result: CheckResult) -> None:
    await deps.github.finish_check(
        repo_of(pull), check_id, conclusion=result.conclusion, title=result.title, summary=result.summary
    )


async def review_and_post(deps: ReviewDeps, pull: PullRequestUpdated) -> CheckResult:
    """Never raises: every outcome, including a crash, becomes the check run's result."""
    try:
        return await _review(deps, repo_of(pull), pull)
    except Superseded:
        return CheckResult("Superseded", "A newer push replaced this commit before the review finished.", "neutral")
    except Exception:
        logger.exception("reviewing %s/%s#%d at %s failed", pull.owner, pull.repo, pull.number, pull.head_sha[:7])
        return CheckResult(
            "Review failed",
            "The review could not be completed. Push again to retry; the bot's logs have the details.",
            "neutral",
        )


async def _review(deps: ReviewDeps, repo: Repo, pull: PullRequestUpdated) -> CheckResult:
    state = await _current(deps, repo, pull)
    base = await deps.github.merge_base(repo, state.base_sha, pull.head_sha)

    with tempfile.TemporaryDirectory(prefix="review-", dir=deps.work_dir) as scratch:
        workspace = await deps.source.checkout(repo, head=pull.head_sha, base=base, dest=Path(scratch) / "repo")
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
        lens, verifier = deps.agents(workspace)
        pr = PullRequestText(state.title, state.body, selection.reviewable, selection.skipped, patches)
        outcome = await review(pr, lens, verifier)

    await _current(deps, repo, pull)  # the review took minutes; don't post against a commit that's gone
    await _post(deps, repo, pull, outcome)
    return _summary(outcome)


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
    # The verdict is computed, never generated: any confirmed finding blocks the merge.
    event = "REQUEST_CHANGES" if outcome.blocking else "APPROVE"
    try:
        await deps.github.create_review(
            repo,
            pull.number,
            commit_sha=pull.head_sha,
            event=event,
            body=review_body(outcome.inline, outcome.general, footer=footer),
            comments=[ReviewComment(f.path, f.line, comment_body(f)) for f in outcome.inline],
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
            body=review_body([], outcome.inline + outcome.general, footer=footer),
            comments=[],
        )


def _summary(outcome: Outcome) -> CheckResult:
    dropped = f" {len(outcome.rejected)} candidate finding(s) were dropped by verification." if outcome.rejected else ""
    if outcome.blocking:
        count = len(outcome.inline) + len(outcome.general)
        return CheckResult(
            f"{count} correctness issue(s)", f"Changes requested; see the review comments.{dropped}", "failure"
        )
    return CheckResult("No correctness issues", f"Approved.{dropped}", "success")
