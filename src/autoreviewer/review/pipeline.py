"""lens -> verify -> dedupe -> anchor. No GitHub here: input is the PR's text and git facts, output is findings.

The models explore the change with their git and file tools; the prompts carry only the PR's text and
the list of changed files, so prompt size no longer grows with the diff.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage

from autoreviewer.review.agents import LENS_LIMITS, VERIFIER_LIMITS
from autoreviewer.review.compaction import compaction_stats
from autoreviewer.review.diff import ChangedPath, commentable_lines
from autoreviewer.review.memory import PriorReview
from autoreviewer.review.schemas import Finding, LensReport, Verification

logger = logging.getLogger("autoreviewer.review")

MAX_FINDINGS = 10
MAX_DESCRIPTION_CHARS = 4000
MAX_LISTED_FILES = 300
VERIFY_CONCURRENCY = 3


@dataclass(frozen=True)
class PullRequestText:
    title: str
    body: str
    reviewable: list[ChangedPath]
    skipped: list[ChangedPath]
    patches: dict[str, str]
    """Hunks per reviewable path, from git: decides which lines take an inline comment."""


@dataclass
class Outcome:
    inline: list[Finding] = field(default_factory=list[Finding])
    """Confirmed and on a line GitHub accepts a review comment on."""
    general: list[Finding] = field(default_factory=list[Finding])
    """Confirmed, but outside the diff hunks: goes in the review body."""
    rejected: list[tuple[Finding, str]] = field(default_factory=list[tuple[Finding, str]])
    """The verifier's verdict was "not a bug" (or the path isn't in the PR)."""
    unverified: list[tuple[Finding, str]] = field(default_factory=list[tuple[Finding, str]])
    """The verifier never reached a verdict (e.g. the model server failed). Not posted, but not judged
    either: later reviews must check these again rather than treat them as rejected."""

    @property
    def blocking(self) -> bool:
        return bool(self.inline or self.general)


@dataclass(frozen=True)
class Continuation:
    """Continue the last review of this PR instead of starting over."""

    prior: PriorReview
    history: list[ModelMessage]
    head_sha: str
    changed_since: list[ChangedPath]
    """What changed between the head the last review saw and this one."""


def _row(change: ChangedPath) -> str:
    return f"- {change.status} {change.path} (+{change.added} -{change.removed})"


def lens_prompt(pr: PullRequestText) -> str:
    listed = pr.reviewable[:MAX_LISTED_FILES]
    more = len(pr.reviewable) - len(listed)
    lines = [
        f"# Pull request\n\nTitle: {pr.title}\n\nDescription:\n{pr.body[:MAX_DESCRIPTION_CHARS] or '(none)'}\n",
        f"# Changed files to review ({len(pr.reviewable)})\n",
        *(_row(change) for change in listed),
    ]
    if more:
        lines.append(f"- ... and {more} more: call changed_files() for the full list")
    if pr.skipped:
        lines.append(f"\n{len(pr.skipped)} generated, vendored, binary, or deleted file(s) are not reviewed.")
    lines.append("\nStart with diff(path) for each file above.")
    return "\n".join(lines)


def continuation_prompt(pr: PullRequestText, continuation: Continuation) -> str:
    prior = continuation.prior
    listed = continuation.changed_since[:MAX_LISTED_FILES]
    more = len(continuation.changed_since) - len(listed)
    lines = [
        f"# New commits\n\nThe pull request moved from `{prior.head_sha[:7]}` (your last review) to "
        f"`{continuation.head_sha[:7]}`. Tool results earlier in this conversation describe the old head; the "
        "checkout and every tool now show the new one.\n",
        f"Title: {pr.title}\n",
        f"# Changed since your last review ({len(continuation.changed_since)} files)\n",
        *(_row(change) for change in listed),
    ]
    if more:
        lines.append(f"- ... and {more} more: use diff_since_last_review() to see them")
    lines.append("\n# Findings you posted last time (line numbers refer to the old head)\n")
    lines.extend(f"- {f['path']}:{f['line']} {f['title']}" for f in prior.findings)
    if not prior.findings:
        lines.append("- none")
    if prior.unverified:
        lines.append("\n# Candidates that could not be verified last time (check them again)\n")
        lines.extend(f"- {f['path']}:{f['line']} {f['title']}" for f in prior.unverified)
    if prior.dropped:
        lines.append("\n# Candidates the verifier rejected last time (don't re-report unless the code changed)\n")
        lines.extend(f"- {f['path']}:{f['line']} {f['title']}: {f['reason']}" for f in prior.dropped)
    lines.append(
        "\nRe-check each finding above against the new code (it may be fixed, moved, or still there), review "
        "what changed with diff_since_last_review(path), and return the complete list of findings for the PR "
        "as it is now, with line numbers in the new head."
    )
    return "\n".join(lines)


def verifier_prompt(pr: PullRequestText, finding: Finding) -> str:
    return (
        f"# Reported finding\n```json\n{json.dumps(finding.model_dump(), indent=2)}\n```\n\n"
        f"# Pull request title\n{pr.title}\n\n"
        f'Check it: start with diff(path="{finding.path}") and read the surrounding code.\n'
    )


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, int]] = set()
    unique = []
    for finding in sorted(findings, key=lambda f: f.severity != "high"):  # keep the high one on a tie
        if (key := (finding.path, finding.line)) not in seen:
            seen.add(key)
            unique.append(finding)
    return unique[:MAX_FINDINGS]


async def review(
    pr: PullRequestText,
    lens: Agent[None, LensReport],
    verifier: Agent[None, Verification],
    *,
    continuation: Continuation | None = None,
    lens_capabilities: Sequence[AbstractCapability[None]] = (),
    conversation: str | None = None,
) -> Outcome:
    prompt = continuation_prompt(pr, continuation) if continuation else lens_prompt(pr)
    lens_run = await lens.run(
        prompt,
        message_history=continuation.history if continuation else None,
        capabilities=list(lens_capabilities) or None,
        conversation_id=conversation,
        # The history names the last review's checkout; this review has a new one (the new head).
        workspace="new" if continuation else None,
        usage_limits=LENS_LIMITS,
    )
    report, usage = lens_run.output, lens_run.usage
    outcome = Outcome()
    reviewable = {change.path for change in pr.reviewable}

    candidates = []
    for finding in _dedupe(report.findings):
        if finding.path in reviewable:
            candidates.append(finding)
        else:  # a path the PR doesn't change: hallucinated or out of scope, not worth a verifier call
            outcome.rejected.append((finding, "path is not among the changed files"))

    slots = asyncio.Semaphore(VERIFY_CONCURRENCY)

    async def verify(finding: Finding) -> Verification | str:
        """The verdict, or why there is none."""
        async with slots:
            try:
                return (await verifier.run(verifier_prompt(pr, finding), usage_limits=VERIFIER_LIMITS)).output
            except Exception as error:  # precision first: an unverifiable finding is not posted
                logger.warning("verifying %s:%s failed: %r", finding.path, finding.line, error)
                return f"verification failed: {type(error).__name__}"

    verdicts = await asyncio.gather(*(verify(finding) for finding in candidates))
    for finding, verdict in zip(candidates, verdicts, strict=True):
        if isinstance(verdict, str):
            outcome.unverified.append((finding, verdict))
        elif not verdict.confirmed:
            outcome.rejected.append((finding, verdict.reason))
        elif finding.line in commentable_lines(pr.patches.get(finding.path, "")):
            outcome.inline.append(finding)
        else:
            outcome.general.append(finding)
    compacted = compaction_stats(lens_run.all_messages())
    logger.info(
        "review: lens made %d requests and %d tool calls (compaction cleared %d, deduplicated %d results); "
        "%d found, %d inline, %d general, %d rejected, %d unverified",
        usage.requests,
        usage.tool_calls,
        compacted["cleared"],
        compacted["superseded"],
        len(report.findings),
        len(outcome.inline),
        len(outcome.general),
        len(outcome.rejected),
        len(outcome.unverified),
    )
    return outcome
