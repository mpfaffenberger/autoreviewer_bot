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

from autoreviewer.github import (
    PostedComment,
    PullRequests,
    Repo,
    ReviewComment,
    ReviewEvent,
    ReviewRejected,
    ReviewThread,
)
from autoreviewer.review.pipeline import Outcome, Withdrawal, blocks, blocks_at, normal_title, same_defect
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


WITHDRAWN_MARKER = "<!-- autoreviewer:withdrawn -->"
"""The last line of an answer that withdrew its thread's finding after re-checking the code (conversation.py)."""
_ANSWER_END = re.compile(r"<!-- autoreviewer:answers (\d+) -->(\n<!-- autoreviewer:withdrawn -->)?\Z")
"""Only the markers that end an answer count: code appends them after the model's text, so nothing the model was
led to write can claim an answer or withdraw a finding."""
_VERDICT_STATE = re.compile(
    r"<!-- autoreviewer:state (APPROVE|REQUEST_CHANGES|COMMENT) outside=(\d+) complete=([01]) -->\n"
    + re.escape(VERDICT_MARKER)
    + r"\Z"
)
"""The line before a verdict's own marker, at the very end of its body (anchored there for the same reason): its
event, how many blocking findings it listed with no thread to withdraw them in, and whether the review covered
the whole change. A withdrawal only ever overturns a complete verdict that requested changes with outside=0."""
_TITLE = re.compile(r"\A\*\*\[P[0-3]\] ([^\n]*)\*\* \(")


Placement = tuple[str, int, str]
"""(path, line on the current head, kind): where GitHub shows a finding comment."""


def answer_markers(comment_id: int, *, withdrawn: bool) -> str:
    """What ends an answer to `comment_id` (appended by code, after the model's text)."""
    return f"<!-- autoreviewer:answers {comment_id} -->" + (f"\n{WITHDRAWN_MARKER}" if withdrawn else "")


def answered(body: str) -> tuple[int, bool] | None:
    """The id of the comment an answer answers, and whether it withdrew the finding, read from the markers that
    end it; None if it doesn't end with them."""
    return (int(match[1]), match[2] is not None) if (match := _ANSWER_END.search(body)) else None


def withdrawn_threads(comments: Iterable[PostedComment]) -> dict[int, PostedComment]:
    """The bot's finding comments whose thread withdrew them, by id: the latest answer decides, so a later answer
    can reinstate a finding."""
    comments = sorted(comments, key=lambda comment: comment.id)
    latest = {c.in_reply_to: answer for c in comments if c.in_reply_to is not None and (answer := answered(c.body))}
    findings = {c.id: c for c in comments if c.in_reply_to is None and finding_marker(c.body)}
    return {root: findings[root] for root, (_, withdrew) in latest.items() if withdrew and root in findings}


def withdrawals(comments: Iterable[PostedComment]) -> tuple[Withdrawal, ...]:
    """The withdrawn findings GitHub still shows on the current head, as a later review should see them."""
    found = []
    for root in withdrawn_threads(comments).values():
        match, title = finding_marker(root.body), _TITLE.match(root.body)
        if root.line is not None and match and match[3] and title:  # outdated, or from before kinds were recorded
            found.append(Withdrawal(root.path, root.line, match[3], int(match[2]), normal_title(title[1])))
    return tuple(found)


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
        """What this review posted, or found already standing on the PR for the same defect."""
        self._existing: dict[str, int] | None = None
        self._placed: dict[Placement, int] = {}
        """(path, line on this head, kind) -> the most urgent priority of the comments of that kind GitHub still
        shows there, from any commit: a superseded review's comments cover the same defect on the new head. Only
        markers that record a kind count (older ones can't be placed)."""
        self._lock = asyncio.Lock()

    @property
    def findings(self) -> list[Finding]:
        """What this review's comments say, so the verdict counts them even if the lens behind one failed."""
        return list(self._findings)

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

    async def withdrawn(self) -> tuple[Withdrawal, ...]:
        """The findings withdrawn in conversation, read afresh (an answer may have come in during the review). Empty,
        and logged, if the PR's comments can't be read: the verdict then counts every finding."""
        try:
            return withdrawals(await self._github.posted_comments(self._repo, self._number, author=self._author))
        except Exception:
            logger.warning("could not read the PR's comments; withdrawals not applied", exc_info=True)
            return ()

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
    event: ReviewEvent = "REQUEST_CHANGES" if outcome.blocking else "COMMENT" if partial else "APPROVE"
    unposted = [f for f in outcome.inline if not covered(posted or {}, fingerprint(head_sha, f), f)]

    def footer(outside: int) -> str:
        state = state_marker(event, outside=outside, complete=not outcome.cut_short)
        return verdict_footer(models_note, head_sha, state, marker)

    outside = sum(outcome.is_blocking(f) for f in outcome.general)
    try:
        await github.create_review(
            repo,
            number,
            commit_sha=head_sha,
            event=event,
            body=review_body(outcome, footer=footer(outside)),
            comments=[inline_comment(f, head_sha=head_sha, block_at=outcome.block_at) for f in unposted],
        )
    except ReviewRejected:
        # GitHub's diff view can differ from ours (e.g. files it doesn't render). Keep every finding, just
        # not inline: same verdict, the unposted findings listed in the review body (with no thread: outside).
        logger.warning("inline comments rejected for %s/%s#%d; listing them in the body", repo.owner, repo.name, number)
        listed = replace(outcome, inline=[], general=[*outcome.general, *outcome.inline])
        outside += sum(outcome.is_blocking(f) for f in unposted)
        await github.create_review(
            repo,
            number,
            commit_sha=head_sha,
            event=event,
            body=review_body(listed, footer=footer(outside)),
            comments=[],
        )


def state_marker(event: ReviewEvent, *, outside: int, complete: bool) -> str:
    return f"<!-- autoreviewer:state {event} outside={outside} complete={int(complete)} -->"


def verdict_footer(note: str, head_sha: str, state: str, marker: str) -> str:
    """What ends every verdict: who and what reviewed, then the state line, then the marker, last."""
    return f"<sub>autoreviewer · {note} · `{head_sha[:7]}`</sub>\n\n{state}\n{marker}"


async def reconsider(github: PullRequests, repo: Repo, number: int, *, author: str, block_at: int) -> bool:
    """After a withdrawal: approve the current head if its latest verdict requested changes after a complete
    review, listed no blocking finding without a thread (outside=0), and no blocking finding comment on it is
    left standing (each was withdrawn in its own thread). Decided by code from the PR's comments, never by the
    model; True if it approved. A replay finds the approval as the latest verdict and does nothing."""
    state = await github.get_pull(repo, number)
    if state.state != "open":
        return False
    bodies = await github.review_bodies(repo, number, commit_sha=state.head_sha, author=author)
    verdicts = [match.groups() for body in bodies if (match := _VERDICT_STATE.search(body))]
    if not verdicts or verdicts[-1] != ("REQUEST_CHANGES", "0", "1"):
        return False
    comments = await github.posted_comments(repo, number, author=author)
    withdrawn = withdrawn_threads(comments)
    standing = [
        comment
        for comment in comments
        if comment.in_reply_to is None
        and comment.line is not None
        and comment.id not in withdrawn
        and (match := finding_marker(comment.body))
        and blocks_at(match[3], int(match[2]), block_at)
    ]
    if standing:
        return False
    # The green check first: a retry after a failure here never leaves an approval beside a red check.
    check = await github.start_check(repo, state.head_sha)
    summary = "Each blocking finding was withdrawn in its thread after a re-check of the code."
    await github.finish_check(repo, check, conclusion="success", title="Approved", summary=summary)
    state_line = state_marker("APPROVE", outside=0, complete=True)
    footer = verdict_footer("re-checked in conversation", state.head_sha, state_line, VERDICT_MARKER)
    body = f"No blocking finding is left standing on this commit. {summary}\n\n{footer}"
    await github.create_review(repo, number, commit_sha=state.head_sha, event="APPROVE", body=body, comments=[])
    logger.info("%s/%s#%d at %s: approved after withdrawals", repo.owner, repo.name, number, state.head_sha[:7])
    return True


async def resolve_fixed(
    github: PullRequests, repo: Repo, number: int, *, head_sha: str, author: str, outcome: Outcome
) -> list[int]:
    """Resolve the threads of the bot's findings the code has moved past, after a review of `head_sha`: GitHub
    shows the comment as outdated (the code under it changed), this review checked that kind in full
    (Outcome.covered), and it reported nothing of that kind in that file, in any state. A note says so first.
    Returns their comment ids.

    Conservative on purpose: a reworded finding, or another one in the file, keeps the thread open."""
    reported = {(f.kind, f.path) for f in outcome.reported}
    gone: list[int] = []
    for comment in await github.posted_comments(repo, number, author=author):
        if comment.in_reply_to is not None or comment.line is not None or not (match := finding_marker(comment.body)):
            continue  # a reply, still on the head's code, or not a finding
        if (kind := match[3]) in outcome.covered and (kind, comment.path) not in reported:
            gone.append(comment.id)
    threads = await _threads_by_root(github, repo, number) if gone else {}
    resolved = []
    for root in gone:
        if (thread := threads.get(root)) is None or thread.resolved:
            continue  # resolved already: by its author, or by an earlier run of this
        note = (
            f"The code here changed, and the review of `{head_sha[:7]}` found nothing like this in the file: resolving."
        )
        await github.reply(repo, number, root, note)
        await github.set_thread_resolved(repo, thread.id, resolved=True)
        resolved.append(root)
    logger.info(
        "%s/%s#%d at %s: resolved %d fixed finding(s)", repo.owner, repo.name, number, head_sha[:7], len(resolved)
    )
    return resolved


async def mark_thread(github: PullRequests, repo: Repo, number: int, root_id: int, *, resolved: bool) -> None:
    """Resolve, or reopen, the thread under comment `root_id`, unless it's that way already."""
    thread = (await _threads_by_root(github, repo, number)).get(root_id)
    if thread is not None and thread.resolved != resolved:
        await github.set_thread_resolved(repo, thread.id, resolved=resolved)


async def _threads_by_root(github: PullRequests, repo: Repo, number: int) -> dict[int, ReviewThread]:
    return {thread.root_comment_id: thread for thread in await github.review_threads(repo, number)}
