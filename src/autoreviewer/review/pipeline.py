"""lens -> verify -> dedupe -> anchor. No GitHub here: input is the PR's text and git facts, output is findings.

The models explore the change with their git and file tools; the prompts carry only the PR's text and
the list of changed files, so prompt size no longer grows with the diff.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from pydantic_ai import Agent

from autoreviewer.review.agents import LENS_LIMITS, VERIFIER_LIMITS
from autoreviewer.review.compaction import compaction_stats
from autoreviewer.review.diff import ChangedPath, commentable_lines
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

    @property
    def blocking(self) -> bool:
        return bool(self.inline or self.general)


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


async def review(pr: PullRequestText, lens: Agent[None, LensReport], verifier: Agent[None, Verification]) -> Outcome:
    lens_run = await lens.run(lens_prompt(pr), usage_limits=LENS_LIMITS)
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

    async def verify(finding: Finding) -> Verification:
        async with slots:
            try:
                return (await verifier.run(verifier_prompt(pr, finding), usage_limits=VERIFIER_LIMITS)).output
            except Exception as error:  # precision first: an unverifiable finding is not posted
                logger.warning("verifying %s:%s failed: %r", finding.path, finding.line, error)
                return Verification(confirmed=False, reason=f"verification failed: {type(error).__name__}")

    verdicts = await asyncio.gather(*(verify(finding) for finding in candidates))
    for finding, verdict in zip(candidates, verdicts, strict=True):
        if not verdict.confirmed:
            outcome.rejected.append((finding, verdict.reason))
        elif finding.line in commentable_lines(pr.patches.get(finding.path, "")):
            outcome.inline.append(finding)
        else:
            outcome.general.append(finding)
    compacted = compaction_stats(lens_run.all_messages())
    logger.info(
        "review: lens made %d requests and %d tool calls (compaction cleared %d, deduplicated %d results); "
        "%d found, %d inline, %d general, %d rejected",
        usage.requests,
        usage.tool_calls,
        compacted["cleared"],
        compacted["superseded"],
        len(report.findings),
        len(outcome.inline),
        len(outcome.general),
        len(outcome.rejected),
    )
    return outcome
