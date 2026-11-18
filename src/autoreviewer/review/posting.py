"""Getting findings onto the PR: each one the moment it's confirmed, then the verdict.

Findings are posted as individual inline comments as they're confirmed, so the author (often another bot)
can start fixing while the review continues. The verdict comes last, as one review (REQUEST_CHANGES or
APPROVE) whose inline comments are only the findings that couldn't be posted earlier.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Collection
from dataclasses import replace

from autoreviewer.github import PullRequests, Repo, ReviewComment, ReviewRejected
from autoreviewer.review.pipeline import Outcome, blocks, same_defect
from autoreviewer.review.render import comment_body, review_body
from autoreviewer.review.schemas import Finding

logger = logging.getLogger("autoreviewer.posting")

_MARKER = re.compile(r"<!-- autoreviewer:([0-9a-f]{16}) -->")


def fingerprint(head_sha: str, finding: Finding) -> str:
    """Identifies a finding's comment on one commit, so replaying a review never posts it twice."""
    return hashlib.sha256(f"{head_sha}|{finding.kind}|{finding.path}|{finding.line}".encode()).hexdigest()[:16]


def inline_comment(finding: Finding, *, head_sha: str, block_at: int) -> ReviewComment:
    body = comment_body(finding, blocking=blocks(finding, block_at))
    return ReviewComment(
        finding.path,
        finding.line,
        f"{body}\n\n<!-- autoreviewer:{fingerprint(head_sha, finding)} -->",
        finding.start_line,
    )


class Publisher:
    """Posts each confirmed inline finding as its own comment, at once. Never raises: a finding that fails to
    post here goes into the final review instead. Safe to replay: comments already on the PR (found by their
    fingerprint) aren't posted again."""

    def __init__(self, github: PullRequests, repo: Repo, number: int, *, head_sha: str, author: str, block_at: int):
        self._github, self._repo, self._number = github, repo, number
        self._head_sha, self._author, self._block_at = head_sha, author, block_at
        self.posted: set[str] = set()
        self._findings: list[Finding] = []
        self._existing: set[str] | None = None
        self._lock = asyncio.Lock()

    async def __call__(self, finding: Finding) -> None:
        key = fingerprint(self._head_sha, finding)
        try:
            async with self._lock:
                if self._existing is None:
                    bodies = await self._github.review_comment_bodies(self._repo, self._number, author=self._author)
                    self._existing = {match for body in bodies for match in _MARKER.findall(body)}
                if key in self._existing or key in self.posted:
                    self.posted.add(key)
                    return
                # Already posted as at least as urgent by the other lens: a second comment would only repeat it.
                if any(same_defect(f, finding) and f.priority <= finding.priority for f in self._findings):
                    self.posted.add(key)
                    return
                self._findings.append(finding)
            comment = inline_comment(finding, head_sha=self._head_sha, block_at=self._block_at)
            await self._github.create_review_comment(
                self._repo, self._number, commit_sha=self._head_sha, comment=comment
            )
        except Exception:
            logger.warning(
                "could not post %s:%s now; it goes in the final review", finding.path, finding.line, exc_info=True
            )
            return
        self.posted.add(key)


async def post_review(
    github: PullRequests,
    repo: Repo,
    number: int,
    *,
    head_sha: str,
    author: str,
    models_note: str,
    outcome: Outcome,
    posted: Collection[str] = (),
) -> None:
    """The verdict, computed and never generated: a confirmed finding at the blocking priority blocks the
    merge. Inline findings already posted (`posted` fingerprints) aren't repeated."""
    if await github.has_review(repo, number, commit_sha=head_sha, author=author):
        # A replay after a crash between posting and checkpointing: the review is already there.
        logger.info("%s/%s#%d at %s already reviewed; not posting again", repo.owner, repo.name, number, head_sha[:7])
        return
    footer = f"<sub>autoreviewer · {models_note} · `{head_sha[:7]}`</sub>"
    event = "REQUEST_CHANGES" if outcome.blocking else "APPROVE"
    unposted = [f for f in outcome.inline if fingerprint(head_sha, f) not in posted]
    try:
        await github.create_review(
            repo,
            number,
            commit_sha=head_sha,
            event=event,
            body=review_body(outcome, footer=footer),
            comments=[inline_comment(f, head_sha=head_sha, block_at=outcome.block_at) for f in unposted],
        )
    except ReviewRejected:
        # GitHub's diff view can differ from ours (e.g. files it doesn't render). Keep every finding, just
        # not inline: same verdict, the unposted findings listed in the review body.
        logger.warning("inline comments rejected for %s/%s#%d; listing them in the body", repo.owner, repo.name, number)
        listed = replace(outcome, inline=[], general=[*outcome.general, *unposted])
        await github.create_review(
            repo, number, commit_sha=head_sha, event=event, body=review_body(listed, footer=footer), comments=[]
        )
