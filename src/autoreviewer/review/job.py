"""One PR push, reviewed: check run -> fetch -> review -> stale-head check -> review + verdict -> check run."""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import Agent

from autoreviewer.github import Conclusion, PullRequests, PullRequestState, Repo, ReviewComment
from autoreviewer.review.checkout import extract_async
from autoreviewer.review.diff import MAX_CHANGED_LINES, select
from autoreviewer.review.pipeline import Outcome, PullRequestText, review
from autoreviewer.review.render import comment_body, review_body
from autoreviewer.review.schemas import LensReport, Verification
from autoreviewer.web.routing import PullRequestUpdated

logger = logging.getLogger("autoreviewer.job")

AgentFactory = Callable[[Path], tuple[Agent[None, LensReport], Agent[None, Verification]]]


@dataclass(frozen=True)
class ReviewDeps:
    github: PullRequests
    agents: AgentFactory
    """Build (lens, verifier) over a checkout directory."""
    work_dir: Path
    models_note: str
    """Shown in the review footer, e.g. "correctness by qwen3.8-flash-next, verified by deepseek-v4.1-flash"."""


class Superseded(Exception):
    """The PR moved on (new push, closed) while we worked; the newer event gets its own review."""


async def review_pull_request(deps: ReviewDeps, pull: PullRequestUpdated) -> None:
    repo = Repo(pull.installation_id, pull.owner, pull.repo)
    check_id = await deps.github.start_check(repo, pull.head_sha)
    conclusion: Conclusion
    try:
        title, summary, conclusion = await _review(deps, repo, pull)
    except Superseded:
        title, summary, conclusion = (
            "Superseded",
            "A newer push replaced this commit before the review finished.",
            "neutral",
        )
    except Exception:
        logger.exception("reviewing %s/%s#%d at %s failed", pull.owner, pull.repo, pull.number, pull.head_sha[:7])
        title, summary, conclusion = (
            "Review failed",
            "The review could not be completed. Push again to retry; the bot's logs have the details.",
            "neutral",
        )
    await deps.github.finish_check(repo, check_id, conclusion=conclusion, title=title, summary=summary)


async def _review(deps: ReviewDeps, repo: Repo, pull: PullRequestUpdated) -> tuple[str, str, Conclusion]:
    state = await _current(deps, repo, pull)
    selection = select(await deps.github.list_files(repo, pull.number))
    if not selection.reviewable:
        return "Nothing to review", "Only removed, generated, vendored, or binary files changed.", "success"
    if selection.changed_lines > MAX_CHANGED_LINES:
        return (
            "Too large to review",
            f"{selection.changed_lines} changed lines; the limit is {MAX_CHANGED_LINES}. Smaller PRs get real reviews.",
            "neutral",
        )

    with tempfile.TemporaryDirectory(prefix="review-", dir=deps.work_dir) as scratch:
        root = await extract_async(await deps.github.download_tarball(repo, pull.head_sha), Path(scratch))
        lens, verifier = deps.agents(root)
        outcome = await review(PullRequestText(state.title, state.body, selection.reviewable), lens, verifier)

    await _current(deps, repo, pull)  # the review took minutes; don't post against a commit that's gone
    await _post(deps, repo, pull, outcome)
    return _summary(outcome)


async def _current(deps: ReviewDeps, repo: Repo, pull: PullRequestUpdated) -> PullRequestState:
    state = await deps.github.get_pull(repo, pull.number)
    if state.head_sha != pull.head_sha or state.state != "open":
        raise Superseded
    return state


async def _post(deps: ReviewDeps, repo: Repo, pull: PullRequestUpdated, outcome: Outcome) -> None:
    footer = f"<sub>autoreviewer · {deps.models_note} · `{pull.head_sha[:7]}`</sub>"
    await deps.github.create_review(
        repo,
        pull.number,
        commit_sha=pull.head_sha,
        # The verdict is computed, never generated: any confirmed finding blocks the merge.
        event="REQUEST_CHANGES" if outcome.blocking else "APPROVE",
        body=review_body(outcome.inline, outcome.general, footer=footer),
        comments=[ReviewComment(f.path, f.line, comment_body(f)) for f in outcome.inline],
    )


def _summary(outcome: Outcome) -> tuple[str, str, Conclusion]:
    dropped = f" {len(outcome.rejected)} candidate finding(s) were dropped by verification." if outcome.rejected else ""
    if outcome.blocking:
        count = len(outcome.inline) + len(outcome.general)
        return f"{count} correctness issue(s)", f"Changes requested; see the review comments.{dropped}", "failure"
    return "No correctness issues", f"Approved.{dropped}", "success"
