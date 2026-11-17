"""Optional one-click simplifications of the code a PR adds, guided by pstack's principles.

A model proposes exact replacements, code rejects any it can prove wrong (outside the PR's added lines,
not shorter, unparseable, overlapping, or carrying a secret), and a second model checks the rest
behave identically. What survives is posted as GitHub suggestions: never blocking, and a failure
anywhere here costs only the suggestions, never the review.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic_ai import Agent

from autoreviewer.review.agents import LENS_LIMITS, VERIFIER_LIMITS
from autoreviewer.review.diff import added_lines
from autoreviewer.review.pipeline import PullRequestText, lens_prompt
from autoreviewer.review.progress import Progress
from autoreviewer.review.render import has_secret
from autoreviewer.review.schemas import Finding, SimplifyReport, SimplifyVerdict, Suggestion
from autoreviewer.review.workspace import Workspace

logger = logging.getLogger("autoreviewer.simplify")

MAX_SUGGESTIONS = 15
CHECK_CONCURRENCY = 3


@dataclass(frozen=True)
class Simplifier:
    propose: Agent[None, SimplifyReport]
    check: Agent[None, SimplifyVerdict]


SimplifierFactory = Callable[[Workspace], Simplifier]


async def suggest(
    pr: PullRequestText, simplifier: Simplifier, root: Path, progress: Progress, *, paths: Collection[str] | None = None
) -> list[Finding]:
    """Checked suggestions for the PR's added lines, limited to `paths` if given (e.g. what changed since
    the last review, so each push doesn't repeat the same suggestions). Never raises: suggestions are
    optional, so any failure here costs only them, never the review."""
    try:
        return await _suggest(pr, simplifier, root, progress, paths)
    except Exception:
        logger.warning("simplifications failed; posting none", exc_info=True)
        return []


async def _suggest(
    pr: PullRequestText, simplifier: Simplifier, root: Path, progress: Progress, paths: Collection[str] | None
) -> list[Finding]:
    scoped = [change for change in pr.reviewable if paths is None or change.path in paths]
    if not scoped:
        return []
    run = await simplifier.propose.run(
        lens_prompt(replace(pr, reviewable=scoped)),
        usage_limits=LENS_LIMITS,
        capabilities=[progress.watch("simplifier")],
    )

    allowed = {change.path for change in scoped}
    accepted: list[Suggestion] = []
    for suggestion in run.output.suggestions:
        if reason := _invalid(suggestion, pr, root, allowed, accepted):
            progress.note(f"dropped suggestion {suggestion.path}:{suggestion.start_line}: {reason}")
        else:
            accepted.append(suggestion)
    accepted = accepted[:MAX_SUGGESTIONS]
    if accepted:
        progress.set_phase(f"Checking {len(accepted)} simplification(s)")

    slots = asyncio.Semaphore(CHECK_CONCURRENCY)

    async def confirmed(suggestion: Suggestion) -> bool:
        async with slots:
            try:
                watch = progress.watch(f"simplify checker ({suggestion.path}:{suggestion.start_line})")
                verdict = (
                    await simplifier.check.run(
                        _check_prompt(suggestion), usage_limits=VERIFIER_LIMITS, capabilities=[watch]
                    )
                ).output
            except Exception:  # unchecked means unposted
                logger.warning("checking a suggestion failed", exc_info=True)
                return False
            progress.note(f"{'kept' if verdict.confirmed else 'dropped'} simplification: {suggestion.title}")
            return verdict.confirmed

    verdicts = await asyncio.gather(*(confirmed(suggestion) for suggestion in accepted))
    return [_finding(suggestion) for suggestion, ok in zip(accepted, verdicts, strict=True) if ok]


def _invalid(
    suggestion: Suggestion, pr: PullRequestText, root: Path, allowed: set[str], accepted: list[Suggestion]
) -> str | None:
    """Why code can rule this suggestion out, or None."""
    start, end = suggestion.start_line, suggestion.end_line
    if suggestion.path not in allowed:
        return "not one of the files in scope"
    if not 1 <= start <= end:
        return "bad line range"
    if not set(range(start, end + 1)) <= added_lines(pr.patches.get(suggestion.path, "")):
        return "touches lines this PR didn't add"
    if any(s.path == suggestion.path and s.start_line <= end and start <= s.end_line for s in accepted):
        return "overlaps another suggestion"
    if has_secret(suggestion.replacement):
        return "contains something that looks like a secret"
    lines = (root / suggestion.path).read_text(errors="replace").splitlines()
    original = "\n".join(lines[start - 1 : end])
    if _weight(suggestion.replacement) >= _weight(original):
        return "not shorter"
    if len(suggestion.replacement.splitlines()) > end - start + 1:
        return "adds lines"
    if suggestion.path.endswith(".py"):
        patched = [*lines[: start - 1], *suggestion.replacement.splitlines(), *lines[end:]]
        if _parses("\n".join(lines)) and not _parses("\n".join(patched)):
            return "the file no longer parses"
    return None


def _weight(code: str) -> int:
    return sum(not char.isspace() for char in code)


def _parses(source: str) -> bool:
    """Parse only: compile with ONLY_AST never runs anything."""
    try:
        compile(source, "<suggestion>", "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)
    except (SyntaxError, ValueError):
        return False
    return True


def _check_prompt(suggestion: Suggestion) -> str:
    return (
        f"File: {suggestion.path}\nReplace lines {suggestion.start_line}-{suggestion.end_line} (new-file numbers) "
        f"with:\n```\n{suggestion.replacement}\n```\n\nClaimed: {suggestion.title}. {suggestion.explanation}"
    )


def _finding(suggestion: Suggestion) -> Finding:
    return Finding(
        path=suggestion.path,
        line=suggestion.end_line,
        start_line=suggestion.start_line,
        replacement=suggestion.replacement.rstrip("\n"),
        priority=3,
        confidence=10,
        title=suggestion.title,
        explanation=suggestion.explanation,
        kind="simplify",
    )
