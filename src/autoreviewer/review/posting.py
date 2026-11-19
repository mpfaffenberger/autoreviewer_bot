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
from collections.abc import Iterable, Mapping
from dataclasses import replace

from autoreviewer.github import PostedComment, PullRequests, Repo, ReviewComment, ReviewEvent, ReviewRejected
from autoreviewer.review.pipeline import Outcome, blocks, same_defect
from autoreviewer.review.render import comment_body, review_body
from autoreviewer.review.schemas import ADVISORY_KINDS, Finding

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


WITHDRAWN_MARKER = "<!-- autoreviewer:withdrawn -->"
"""Ends an answer that withdrew its thread's finding after re-checking the code (conversation.py)."""
_ANSWERS = re.compile(r"<!-- autoreviewer:answers (\d+) -->")
_STATE = re.compile(r"<!-- autoreviewer:state (APPROVE|REQUEST_CHANGES|COMMENT) outside=(\d+) -->")
"""In every verdict's body: its event, and how many blocking findings it listed outside the diff (no thread to
withdraw them in). A withdrawal only ever overturns a verdict that says outside=0."""

Placement = tuple[str, int, str]
"""(path, line on the current head, kind): where GitHub shows a finding comment."""


def answered_marker(comment_id: int) -> str:
    return f"<!-- autoreviewer:answers {comment_id} -->"


def answered(body: str) -> int | None:
    """The comment an answer of the bot's answers, from its marker."""
    return int(match[1]) if (match := _ANSWERS.search(body)) else None


def withdrawn_placements(comments: Iterable[PostedComment]) -> frozenset[Placement]:
    """Where the bot's finding comments stand withdrawn: the latest answer in the thread withdrew the finding
    (a later answer may reinstate it), and GitHub still shows the comment on the current head."""
    comments = sorted(comments, key=lambda comment: comment.id)
    latest: dict[int, PostedComment] = {}
    for comment in comments:
        if comment.in_reply_to is not None and answered(comment.body) is not None:
            latest[comment.in_reply_to] = comment  # a later answer replaces an earlier one
    withdrawn = set[Placement]()
    for root in comments:
        answer, match = latest.get(root.id), finding_marker(root.body)
        if root.in_reply_to is not None or root.line is None or match is None or not match[3]:
            continue  # a reply, outdated, not a finding, or from before markers recorded kinds
        if answer is not None and WITHDRAWN_MARKER in answer.body:
            withdrawn.add((root.path, root.line, match[3]))
    return frozenset(withdrawn)


def finding_marker(body: str) -> re.Match[str] | None:
    """The marker that ends one of the bot's finding comments: (fingerprint, priority, kind or None)."""
    return _MARKER.search(body)


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
        self._comments: list[PostedComment] = []
        self._placed: dict[Placement, int] = {}
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

    async def withdrawn(self) -> frozenset[Placement]:
        """Placements whose findings stand withdrawn in conversation: a finding found there again doesn't block.
        Empty (and logged) if the PR's comments can't be read: the verdict then counts every finding."""
        try:
            async with self._lock:
                if self._existing is None:
                    await self._load_existing()
        except Exception:
            logger.warning("could not read the PR's comments; withdrawals not applied", exc_info=True)
            return frozenset()
        return withdrawn_placements(self._comments)

    async def _load_existing(self) -> dict[str, int]:
        """Fingerprints (and placements) of the bot's comments on the PR. Recorded only once the fetch succeeds,
        so a failed fetch is retried with the next finding instead of switching dedupe off for the review."""
        comments = await self._github.posted_comments(self._repo, self._number, author=self._author)
        self._comments = comments
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
    event: ReviewEvent = "REQUEST_CHANGES" if outcome.blocking else "COMMENT" if partial else "APPROVE"
    unposted = [f for f in outcome.inline if not covered(posted or {}, fingerprint(head_sha, f), f)]
    try:
        await github.create_review(
            repo,
            number,
            commit_sha=head_sha,
            event=event,
            body=review_body(outcome, footer=f"{state_marker(event, outcome)}\n{footer}"),
            comments=[inline_comment(f, head_sha=head_sha, block_at=outcome.block_at) for f in unposted],
        )
    except ReviewRejected:
        # GitHub's diff view can differ from ours (e.g. files it doesn't render). Keep every finding, just
        # not inline: same verdict, the unposted findings listed in the review body.
        logger.warning("inline comments rejected for %s/%s#%d; listing them in the body", repo.owner, repo.name, number)
        listed = replace(outcome, inline=[], general=[*outcome.general, *outcome.inline])
        body = review_body(listed, footer=f"{state_marker(event, listed)}\n{footer}")
        await github.create_review(repo, number, commit_sha=head_sha, event=event, body=body, comments=[])


def state_marker(event: ReviewEvent, outcome: Outcome) -> str:
    outside = sum(outcome.is_blocking(f) for f in outcome.general)
    return f"<!-- autoreviewer:state {event} outside={outside} -->"


async def reconsider(github: PullRequests, repo: Repo, number: int, *, author: str, block_at: int) -> bool:
    """After a withdrawal: approve the current head if its verdict requested changes and no blocking finding is
    left standing. Decided by code from the PR's comments, never by the model; True if it approved.

    Standing: a finding comment GitHub still shows on the head, at the blocking priority, of a kind that can
    block, and not withdrawn. Never overturned: a verdict that listed blocking findings outside the diff (they
    have no thread), or one from before verdicts recorded that (no state marker)."""
    state = await github.get_pull(repo, number)
    bodies = [b for b in await github.review_bodies(repo, number, commit_sha=state.head_sha, author=author) if b]
    verdicts = [match for body in bodies if (match := _STATE.search(body)) and body.endswith(VERDICT_MARKER)]
    if state.state != "open" or not verdicts or verdicts[-1][1] != "REQUEST_CHANGES" or verdicts[-1][2] != "0":
        return False
    comments = await github.posted_comments(repo, number, author=author)
    withdrawn = withdrawn_placements(comments)
    for comment in comments:
        match = finding_marker(comment.body)
        if comment.in_reply_to is not None or comment.line is None or match is None:
            continue
        priority, kind = int(match[2]), match[3]
        if priority <= block_at and kind not in ADVISORY_KINDS and (comment.path, comment.line, kind) not in withdrawn:
            return False  # still standing (a marker without a kind counts: it may well block)
    footer = f"<sub>autoreviewer · re-checked in conversation · `{state.head_sha[:7]}`</sub>\n\n{VERDICT_MARKER}"
    body = (
        "Every blocking finding on this commit was withdrawn after a re-check of the code in its thread.\n\n"
        f"<!-- autoreviewer:state APPROVE outside=0 -->\n{footer}"
    )
    await github.create_review(repo, number, commit_sha=state.head_sha, event="APPROVE", body=body, comments=[])
    check = await github.start_check(repo, state.head_sha)
    await github.finish_check(
        repo,
        check,
        conclusion="success",
        title="Approved",
        summary="Every blocking finding was withdrawn in conversation.",
    )
    logger.info("%s/%s#%d at %s: approved after withdrawals", repo.owner, repo.name, number, state.head_sha[:7])
    return True
