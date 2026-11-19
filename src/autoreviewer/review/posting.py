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
from collections.abc import Mapping
from dataclasses import replace

from autoreviewer.github import PullRequests, Repo, ReviewComment, ReviewRejected
from autoreviewer.review.pipeline import Outcome, blocks, same_defect
from autoreviewer.review.render import comment_body, review_body
from autoreviewer.review.schemas import Finding

logger = logging.getLogger("autoreviewer.posting")

VERDICT_MARKER = "<!-- autoreviewer:verdict -->"
"""Ends every final verdict (approve or request changes), telling it apart from the empty reviews GitHub
creates around single comments, and from a partial one."""
PARTIAL_MARKER = "<!-- autoreviewer:partial -->"
"""Ends a "partially reviewed" COMMENT: it never stops a later full review of the commit from posting."""
_MARKER = re.compile(r"<!-- autoreviewer:([0-9a-f]{16}) p([0-3])(?: ([a-z]+))? -->\Z")
"""Only the marker that ends a comment counts: code appends it last, after any model text or suggestion code,
so a marker an attacker plants in the PR (and gets quoted) can't make a real finding look already posted."""


def fingerprint(head_sha: str, finding: Finding) -> str:
    """Identifies a finding's comment on one commit, so replaying a review never posts it twice. Priority isn't
    part of it (a replay's verifier may rank the same finding differently); the marker records it separately,
    so only a more urgent restatement is posted again."""
    key = f"{head_sha}|{finding.kind}|{finding.path}|{finding.line}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def covered[Key](posted: Mapping[Key, int], key: Key, finding: Finding) -> bool:
    """Whether a comment at least as urgent as `finding` is already posted under `key` (its fingerprint, or where
    GitHub shows a comment of its kind)."""
    return key in posted and posted[key] <= finding.priority


def inline_comment(finding: Finding, *, head_sha: str, block_at: int) -> ReviewComment:
    body = comment_body(finding, blocking=blocks(finding, block_at))
    return ReviewComment(
        finding.path,
        finding.line,
        f"{body}\n\n<!-- autoreviewer:{fingerprint(head_sha, finding)} p{finding.priority} {finding.kind} -->",
        finding.start_line,
    )


class Publisher:
    """Posts each confirmed inline finding as its own comment, at once. Never raises: a finding that fails to
    post here goes into the final review instead. Safe to replay: comments already on the PR (found by their
    fingerprint) aren't posted again."""

    def __init__(self, github: PullRequests, repo: Repo, number: int, *, head_sha: str, author: str, block_at: int):
        self._github, self._repo, self._number = github, repo, number
        self._head_sha, self._author, self._block_at = head_sha, author, block_at
        self.posted: dict[str, int] = {}
        """Fingerprint -> the most urgent priority posted for it (this review, or found on the PR)."""
        self._findings: list[Finding] = []
        self._existing: dict[str, int] | None = None
        self._placed: dict[tuple[str, int, str], int] = {}
        """(path, line on this head, kind) -> the most urgent priority of the comments of that kind GitHub still
        shows there, from any commit: a superseded review's comments cover the same defect on the new head. Only
        markers that record a kind count (older ones can't be placed)."""
        self._lock = asyncio.Lock()

    async def __call__(self, finding: Finding) -> None:
        key = fingerprint(self._head_sha, finding)
        # One post at a time, decided and recorded under the lock: two reports of one defect arriving together
        # must not both see "not posted yet". (Posts are few and quick.)
        async with self._lock:
            try:
                existing = self._existing if self._existing is not None else await self._load_existing()
                if covered(existing, key, finding) or covered(self.posted, key, finding):
                    self.posted[key] = min(finding.priority, existing.get(key, 3), self.posted.get(key, 3))
                    return
                where = (finding.path, finding.line, finding.kind)
                if covered(self._placed, where, finding):  # posted for an earlier commit, still on this line
                    self.posted[key] = self._placed[where]
                    self._findings.append(finding)  # its comment exists: it stands in for the other lens's too
                    return
                # Already posted as at least as urgent by the other lens: a second comment would only repeat it.
                if any(same_defect(f, finding) and f.priority <= finding.priority for f in self._findings):
                    self.posted[key] = finding.priority
                    return
                comment = inline_comment(finding, head_sha=self._head_sha, block_at=self._block_at)
                await self._github.create_review_comment(
                    self._repo, self._number, commit_sha=self._head_sha, comment=comment
                )
            except Exception:
                logger.warning(
                    "could not post %s:%s now; it goes in the final review", finding.path, finding.line, exc_info=True
                )
                return
            self.posted[key] = finding.priority
            self._findings.append(finding)  # only once its comment exists can it stand in for a duplicate

    async def _load_existing(self) -> dict[str, int]:
        """Fingerprints (and placements) of the bot's comments on the PR. Recorded only once the fetch succeeds,
        so a failed fetch is retried with the next finding instead of switching dedupe off for the review."""
        comments = await self._github.posted_comments(self._repo, self._number, author=self._author)
        existing: dict[str, int] = {}
        for comment in comments:
            if not (match := _MARKER.search(comment.body)):
                continue
            fp, priority, kind = match[1], int(match[2]), match[3]
            existing[fp] = min(priority, existing.get(fp, 3))
            if kind and comment.line is not None:
                where = (comment.path, comment.line, kind)
                self._placed[where] = min(priority, self._placed.get(where, 3))
        self._existing = existing
        return existing


async def post_review(
    github: PullRequests,
    repo: Repo,
    number: int,
    *,
    head_sha: str,
    author: str,
    models_note: str,
    outcome: Outcome,
    posted: Mapping[str, int] | None = None,
) -> None:
    """The verdict, computed and never generated: a confirmed finding at the blocking priority blocks the
    merge. Inline findings already posted (`posted` fingerprints) aren't repeated."""
    # A review cut short by the time limit never approves: it hasn't seen the whole change. Its verdict is
    # partial: a later full review of the same commit (a retry) must still post its own.
    partial = outcome.cut_short and not outcome.blocking
    marker = PARTIAL_MARKER if partial else VERDICT_MARKER
    bodies = await github.review_bodies(repo, number, commit_sha=head_sha, author=author)
    if any(body.endswith(VERDICT_MARKER) or (partial and body.endswith(PARTIAL_MARKER)) for body in bodies):
        # A replay after a crash between posting and checkpointing: this verdict is already there. (GitHub
        # also wraps each early inline comment in an empty review of its own; those never count.)
        logger.info("%s/%s#%d at %s already reviewed; not posting again", repo.owner, repo.name, number, head_sha[:7])
        return
    footer = f"<sub>autoreviewer · {models_note} · `{head_sha[:7]}`</sub>\n\n{marker}"
    event = "REQUEST_CHANGES" if outcome.blocking else "COMMENT" if partial else "APPROVE"
    unposted = [f for f in outcome.inline if not covered(posted or {}, fingerprint(head_sha, f), f)]
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
        listed = replace(outcome, inline=[], general=[*outcome.general, *outcome.inline])
        await github.create_review(
            repo, number, commit_sha=head_sha, event=event, body=review_body(listed, footer=footer), comments=[]
        )
