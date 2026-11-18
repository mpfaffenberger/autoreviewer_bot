"""lens -> verify -> dedupe -> anchor. No GitHub here: input is the PR's text and git facts, output is findings.

The models explore the change with their git and file tools; the prompts carry only the PR's text and
the list of changed files, so prompt size no longer grows with the diff.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic_ai import Agent, AgentRunResult, Tool
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage
from pydantic_ai.toolsets import FunctionToolset

from autoreviewer.review.agents import LENS_LIMITS, REPORT_AS_YOU_GO, REPORT_TOOL, VERIFIER_LIMITS
from autoreviewer.review.compaction import compaction_stats
from autoreviewer.review.diff import ChangedPath, commentable_lines
from autoreviewer.review.memory import PriorReview
from autoreviewer.review.progress import Progress
from autoreviewer.review.schemas import ADVISORY_KINDS, Finding, LensKind, LensReport, Verification

logger = logging.getLogger("autoreviewer.review")

MAX_FINDINGS = 25
MIN_CONFIDENCE = 4
"""Findings below this confidence (PR-Agent's "a hunch") aren't worth a verifier call."""
BLOCK_AT_PRIORITY = 1
"""Confirmed findings at this priority or more urgent (P0, P1) block the merge; the rest are advisory."""
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


def blocks(finding: Finding, block_at: int) -> bool:
    """Whether a confirmed finding blocks the merge: urgent enough, and not merely advice."""
    return finding.kind not in ADVISORY_KINDS and finding.priority <= block_at


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
    low_confidence: list[Finding] = field(default_factory=list[Finding])
    """Below MIN_CONFIDENCE, so never sent to the verifier. Not judged, so never remembered as rejected:
    a later review is free to report them again with more confidence."""
    block_at: int = BLOCK_AT_PRIORITY
    overall_correct: bool = True
    """The lens's overall verdict on the change (Codex-style), alongside its findings."""
    reviewed: list[str] = field(default_factory=list[str])
    added_lines: int = 0
    """Lines the PR adds in reviewable files: what simplifications are measured against."""

    @property
    def posted(self) -> list[Finding]:
        return [*self.inline, *self.general]

    def absorb(self, other: Outcome) -> None:
        """Add another lens's results. Where correctness and security posted on one line (likely one defect
        seen twice), the more urgent finding stays; other kinds (types, DRY, simplify) are separate issues."""
        self.rejected += other.rejected
        self.unverified += other.unverified
        self.low_confidence += other.low_confidence
        for target, findings in ((self.inline, other.inline), (self.general, other.general)):
            for finding in findings:
                if (same := next((f for f in target if same_defect(f, finding)), None)) is None:
                    target.append(finding)
                elif finding.priority < same.priority:
                    target[target.index(same)] = finding

    def is_blocking(self, finding: Finding) -> bool:
        return blocks(finding, self.block_at)

    @property
    def blocking(self) -> bool:
        return any(self.is_blocking(f) for f in self.posted)


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


def same_defect(a: Finding, b: Finding) -> bool:
    """Correctness and security findings on one line: likely one defect seen by two lenses."""
    return {a.kind, b.kind} <= {"correctness", "security"} and (a.path, a.line) == (b.path, b.line)


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, int]] = set()
    unique = []
    # Most urgent, then most confident, first: they win ties on a line and survive the cap.
    for finding in sorted(findings, key=lambda f: (f.priority, -f.confidence)):
        if (key := (finding.path, finding.line)) not in seen:
            seen.add(key)
            unique.append(finding)
    return unique[:MAX_FINDINGS]


def place(outcome: Outcome, finding: Finding, patch: str) -> bool:
    """File `finding` as inline (on a line GitHub can comment on) or general; True if inline."""
    inline = finding.line in commentable_lines(patch)
    (outcome.inline if inline else outcome.general).append(finding)
    return inline


Publish = Callable[[Finding], Awaitable[None]]
"""Post one confirmed inline finding right away (see posting.Publisher). Must not raise."""


class Judge:
    """Verifies a lens's findings as they arrive (from `report_finding` mid-run, then the final report) and
    sorts each into `outcome`. Each confirmed inline finding is handed to `publish` at once."""

    def __init__(
        self,
        pr: PullRequestText,
        verifier: Agent[None, Verification],
        progress: Progress,
        outcome: Outcome,
        *,
        kind: Literal["correctness"] | LensKind = "correctness",
        label: str,
        publish: Publish | None = None,
    ) -> None:
        self._pr, self._verifier, self._progress, self._outcome = pr, verifier, progress, outcome
        self._kind, self._label, self._publish = kind, label, publish
        self._reviewable = {change.path for change in pr.reviewable}
        self._slots = asyncio.Semaphore(VERIFY_CONCURRENCY)
        self._tasks: dict[tuple[str, int], asyncio.Task[None]] = {}

    def toolset(self) -> FunctionToolset[None]:
        async def report(finding: Finding) -> str:
            """Report one finding now, while you keep reviewing. Same fields as a finding in the final report."""
            return self.submit(finding)

        return FunctionToolset([Tool(report, name=REPORT_TOOL)])

    def submit(self, finding: Finding) -> str:
        """Start verifying `finding` unless it's out of scope, a hunch, or its line was already reported."""
        finding = finding.model_copy(update={"kind": self._kind})
        if (key := (finding.path, finding.line)) in self._tasks:
            return "Already reported for this line. Keep reviewing."
        if finding.path not in self._reviewable:  # hallucinated or out of scope, not worth a verifier call
            self._outcome.rejected.append((finding, "path is not among the changed files"))
            return f"{finding.path} is not among the changed files: not reported."
        if finding.confidence < MIN_CONFIDENCE:
            self._outcome.low_confidence.append(finding)
            return "Below the confidence needed to verify it: not reported."
        if len(self._tasks) >= MAX_FINDINGS:
            return f"You have reported {MAX_FINDINGS} findings, the most that are checked: not reported."
        self._tasks[key] = asyncio.create_task(self._verify(finding))
        self._progress.note(f"reported: {finding.title}")
        return "Recorded: it is being verified now and will be posted if confirmed. Keep reviewing."

    async def finish(self, findings: list[Finding]) -> None:
        """Verify the final report's findings that weren't reported along the way, and wait for every verdict."""
        for finding in _dedupe(findings):
            self.submit(finding)
        if pending := [task for task in self._tasks.values() if not task.done()]:
            self._progress.set_phase(f"Verifying {len(pending)} candidate finding(s)")
        await asyncio.gather(*self._tasks.values())

    def cancel(self) -> None:
        for task in self._tasks.values():
            task.cancel()

    async def _verify(self, finding: Finding) -> None:
        async with self._slots:
            try:
                watch = self._progress.watch(f"{self._label} ({finding.path}:{finding.line})")
                prompt = verifier_prompt(self._pr, finding)
                verdict = (await self._verifier.run(prompt, usage_limits=VERIFIER_LIMITS, capabilities=[watch])).output
            except Exception as error:  # precision first: an unverifiable finding is not posted
                logger.warning("verifying %s:%s failed: %r", finding.path, finding.line, error)
                self._outcome.unverified.append((finding, f"verification failed: {type(error).__name__}"))
                return
        self._progress.note(f"{'confirmed' if verdict.confirmed else 'rejected'}: {finding.title}")
        if not verdict.confirmed:
            self._outcome.rejected.append((finding, verdict.reason))
            return
        # The verifier read the code too: its corrections win.
        corrections = {"priority": verdict.priority, "explanation": verdict.explanation}
        finding = finding.model_copy(update={k: v for k, v in corrections.items() if v is not None})
        if place(self._outcome, finding, self._pr.patches.get(finding.path, "")) and self._publish is not None:
            await self._publish(finding)


LensRun = Callable[[list[FunctionToolset[None]]], Awaitable[AgentRunResult[LensReport]]]
"""Starts a lens run with these extra toolsets."""


async def _run_judged(judge: Judge, run: LensRun) -> AgentRunResult[LensReport]:
    """Run a lens with `report_finding`, then judge its final report; no verification outlives a failure."""
    try:
        result = await run([judge.toolset()])
        await judge.finish(result.output.findings)
    except BaseException:
        judge.cancel()
        raise
    return result


async def review_lens(
    pr: PullRequestText,
    lens: Agent[None, LensReport],
    verifier: Agent[None, Verification],
    *,
    kind: LensKind,
    instructions: str | None,
    progress: Progress,
    block_at: int = BLOCK_AT_PRIORITY,
    publish: Publish | None = None,
) -> Outcome:
    """An extra lens (security, DRY) over `pr`, always fresh, checked by its own verifier."""
    outcome = Outcome(block_at=block_at)
    judge = Judge(pr, verifier, progress, outcome, kind=kind, label=f"{kind} verifier", publish=publish)
    combined = "\n\n".join(part for part in (instructions, REPORT_AS_YOU_GO) if part)
    run = await _run_judged(
        judge,
        lambda toolsets: lens.run(
            lens_prompt(pr),
            instructions=combined,
            toolsets=toolsets,
            capabilities=[progress.watch(kind)],
            usage_limits=LENS_LIMITS,
        ),
    )
    logger.info("%s: %d requests, %d posted", kind, run.usage.requests, len(outcome.posted))
    return outcome


async def review(
    pr: PullRequestText,
    lens: Agent[None, LensReport],
    verifier: Agent[None, Verification],
    *,
    continuation: Continuation | None = None,
    lens_capabilities: Sequence[AbstractCapability[None]] = (),
    conversation: str | None = None,
    block_at: int = BLOCK_AT_PRIORITY,
    checked: Sequence[Finding] = (),
    progress: Progress | None = None,
    publish: Publish | None = None,
) -> Outcome:
    progress = progress or Progress()
    outcome = Outcome(block_at=block_at)
    for finding in checked:  # proven by a tool, not a model: nothing to verify, so post at once
        if place(outcome, finding, pr.patches.get(finding.path, "")) and publish is not None:
            await publish(finding)
    prompt = continuation_prompt(pr, continuation) if continuation else lens_prompt(pr)
    progress.set_phase("Continuing the last review" if continuation else "Reviewing the change")
    judge = Judge(pr, verifier, progress, outcome, label="verifier", publish=publish)
    lens_run = await _run_judged(
        judge,
        lambda toolsets: lens.run(
            prompt,
            instructions=REPORT_AS_YOU_GO,
            toolsets=toolsets,
            message_history=continuation.history if continuation else None,
            capabilities=[*lens_capabilities, progress.watch("reviewer")],
            conversation_id=conversation,
            # The history names the last review's checkout; this review has a new one (the new head).
            workspace="new" if continuation else None,
            usage_limits=LENS_LIMITS,
        ),
    )

    report, usage = lens_run.output, lens_run.usage
    outcome.overall_correct, outcome.reviewed = report.overall_correct, report.reviewed
    compacted = compaction_stats(lens_run.all_messages())
    logger.info(
        "review: lens made %d requests and %d tool calls (compaction cleared %d, deduplicated %d results); "
        "%d found, %d inline, %d general, %d rejected, %d unverified, %d low-confidence",
        usage.requests,
        usage.tool_calls,
        compacted["cleared"],
        compacted["superseded"],
        len(report.findings),
        len(outcome.inline),
        len(outcome.general),
        len(outcome.rejected),
        len(outcome.unverified),
        len(outcome.low_confidence),
    )
    return outcome
