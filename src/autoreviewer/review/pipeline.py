"""lens -> verify -> dedupe -> anchor. No GitHub here: input is the PR's text, output is typed findings."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from pydantic_ai import Agent

from autoreviewer.github import ChangedFile
from autoreviewer.review.agents import LENS_LIMITS, VERIFIER_LIMITS
from autoreviewer.review.diff import annotate, commentable_lines
from autoreviewer.review.schemas import Finding, LensReport, Verification

logger = logging.getLogger("autoreviewer.review")

MAX_FINDINGS = 10
MAX_DESCRIPTION_CHARS = 4000
VERIFY_CONCURRENCY = 3


@dataclass(frozen=True)
class PullRequestText:
    title: str
    body: str
    files: list[ChangedFile]
    """Reviewable files only, each with a patch."""


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


def lens_prompt(pr: PullRequestText) -> str:
    parts = [f"# Pull request\n\nTitle: {pr.title}\n\nDescription:\n{pr.body[:MAX_DESCRIPTION_CHARS] or '(none)'}\n"]
    parts.append("# Annotated diff\n")
    for changed in pr.files:
        parts.append(f"## {changed.path} ({changed.status})\n```diff\n{annotate(changed.patch or '')}\n```\n")
    return "\n".join(parts)


def verifier_prompt(pr: PullRequestText, finding: Finding, patch: str) -> str:
    return (
        f"# Reported finding\n```json\n{json.dumps(finding.model_dump(), indent=2)}\n```\n\n"
        f"# Pull request title\n{pr.title}\n\n"
        f"# Annotated diff of {finding.path}\n```diff\n{annotate(patch)}\n```\n"
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
    patches = {changed.path: changed.patch or "" for changed in pr.files}
    report = (await lens.run(lens_prompt(pr), usage_limits=LENS_LIMITS)).output
    outcome = Outcome()

    candidates = []
    for finding in _dedupe(report.findings):
        if finding.path in patches:
            candidates.append(finding)
        else:  # a path the PR doesn't touch: hallucinated or out of scope, not worth a verifier call
            outcome.rejected.append((finding, "path is not among the changed files"))

    slots = asyncio.Semaphore(VERIFY_CONCURRENCY)

    async def verify(finding: Finding) -> Verification:
        async with slots:
            try:
                result = await verifier.run(
                    verifier_prompt(pr, finding, patches[finding.path]), usage_limits=VERIFIER_LIMITS
                )
                return result.output
            except Exception as error:  # precision first: an unverifiable finding is not posted
                logger.warning("verifying %s:%s failed: %r", finding.path, finding.line, error)
                return Verification(confirmed=False, reason=f"verification failed: {type(error).__name__}")

    verdicts = await asyncio.gather(*(verify(finding) for finding in candidates))
    for finding, verdict in zip(candidates, verdicts, strict=True):
        if not verdict.confirmed:
            outcome.rejected.append((finding, verdict.reason))
        elif finding.line in commentable_lines(patches[finding.path]):
            outcome.inline.append(finding)
        else:
            outcome.general.append(finding)
    logger.info(
        "review: %d found, %d inline, %d general, %d rejected",
        len(report.findings), len(outcome.inline), len(outcome.general), len(outcome.rejected),
    )  # fmt: skip
    return outcome
