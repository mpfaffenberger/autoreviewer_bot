"""Deterministic checks: rules a linter decides exactly, so no model guesses at them.

- Type annotations, enforced with ruff on the lines a PR adds.
- Unused imports the PR leaves behind (often by deleting an import's last use, so the import line itself is
  untouched): unused at the head but not at the merge-base. Advisory: failing the build is CI's job.

ruff only parses files (it never runs them), and `--isolated` ignores the repository's own ruff
configuration, so a PR can't switch the rules off or point ruff at files outside the checkout.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from pathlib import Path
from typing import TypedDict

from pydantic import TypeAdapter, ValidationError
from ruff.__main__ import find_ruff_bin

from autoreviewer.review.diff import ChangedPath, added_lines
from autoreviewer.review.schemas import Finding
from autoreviewer.review.workspace import BASE, GitError, run_git

logger = logging.getLogger("autoreviewer.lint")

PYTHON_SUFFIXES = (".py", ".pyi")
TYPE_RULES = "ANN0,ANN2,ANN401,PGH003"
TYPE_RULE_PREFIXES = ("ANN", "PGH003")
"""ruff also reports `invalid-syntax` whatever is selected: not a typing finding (the lens sees broken code)."""
"""Missing argument and return annotations, `Any`, and `# type: ignore` without an error code."""
UNUSED_IMPORT = "F401"
RULES = f"{TYPE_RULES},{UNUSED_IMPORT}"
NO_BASE_FILE = frozenset("ACR")
"""git statuses with no file at this path in the merge-base: every unused import in it is new."""
RUFF_OPTIONS = ("lint.flake8-annotations.mypy-init-return = true",)
"""`__init__` with annotated arguments needs no `-> None`, as in mypy."""
ADVISORY_RULES = frozenset({"ANN401", "PGH003"})
"""Sometimes justified (e.g. `Any` at a JSON boundary): advisory, while a missing annotation blocks."""
MAX_LINT_FINDINGS = 30
RUFF_TIMEOUT_S = 120.0


class LintError(RuntimeError):
    pass


class _Location(TypedDict):
    row: int


class Diagnostic(TypedDict):
    """The part of ruff's JSON output we read."""

    code: str | None
    message: str
    filename: str
    location: _Location


_DIAGNOSTICS = TypeAdapter(list[Diagnostic])


async def lint_findings(root: Path, changes: list[ChangedPath], patches: dict[str, str]) -> list[Finding]:
    """One finding per line that breaks a rule, most urgent first, at most MAX_LINT_FINDINGS."""
    python = {c.path: c for c in changes if c.path.endswith(PYTHON_SUFFIXES)}
    if not python:
        return []
    added = {path: added_lines(patches.get(path, "")) for path in python}
    typed: defaultdict[tuple[str, int], list[Diagnostic]] = defaultdict(list)
    unused: defaultdict[tuple[str, int], list[Diagnostic]] = defaultdict(list)
    for diagnostic in await _ruff(root, sorted(python)):
        code = diagnostic["code"] or ""
        path = Path(diagnostic["filename"]).resolve().relative_to(root.resolve()).as_posix()
        line = diagnostic["location"]["row"]
        if code == UNUSED_IMPORT:
            unused[(path, line)].append(diagnostic)
        elif code.startswith(TYPE_RULE_PREFIXES) and line in added.get(path, ()):
            typed[(path, line)].append(diagnostic)
    findings = [_finding(path, line, hits) for (path, line), hits in typed.items()]
    already = await _unused_at_base(root, [python[path] for path in sorted({path for path, _ in unused})])
    for (path, line), hits in unused.items():
        if fresh := [hit for hit in hits if hit["message"] not in already.get(path, ())]:
            findings.append(_unused_finding(path, line, fresh))
    findings.sort(key=_urgency)
    if len(findings) > MAX_LINT_FINDINGS:
        logger.info("%d lint findings; posting the first %d", len(findings), MAX_LINT_FINDINGS)
    return findings[:MAX_LINT_FINDINGS]


async def _unused_at_base(root: Path, changes: list[ChangedPath]) -> dict[str, set[str]]:
    """Each file's unused-import messages at the merge-base, so only imports the PR left unused are reported."""
    already: dict[str, set[str]] = {}
    for change in changes:
        if change.status in NO_BASE_FILE:
            continue
        try:
            source = await run_git(root, "show", f"{BASE}:{change.path}")
        except GitError as error:
            raise LintError(f"could not read {change.path} at the merge-base: {error}") from error
        diagnostics = await _ruff(root, [change.path], select=UNUSED_IMPORT, source=source)
        already[change.path] = {d["message"] for d in diagnostics if d["code"] == UNUSED_IMPORT}
    return already


def _urgency(finding: Finding) -> tuple[int, str, int]:
    return finding.priority, finding.path, finding.line


def _finding(path: str, line: int, hits: list[Diagnostic]) -> Finding:
    codes = {hit["code"] or "" for hit in hits}
    blocking = bool(codes - ADVISORY_RULES)
    if blocking:
        title = "Add the missing type annotations"
    elif "ANN401" in codes:
        title = "Replace `Any` with a precise type"
    else:
        title = "Name the error code this `# type: ignore` suppresses"
    return Finding(
        path=path,
        line=line,
        priority=1 if blocking else 2,
        confidence=10,
        title=title,
        explanation="\n".join(f"- {hit['message']} (ruff `{hit['code']}`)" for hit in hits),
        kind="types",
    )


def _unused_finding(path: str, line: int, hits: list[Diagnostic]) -> Finding:
    return Finding(
        path=path,
        line=line,
        priority=3,
        confidence=10,
        title="Remove the unused import",
        explanation="\n".join(f"- {hit['message']} (ruff `{hit['code']}`)" for hit in hits),
        kind="lint",
    )


async def _ruff(root: Path, paths: list[str], *, select: str = RULES, source: str | None = None) -> list[Diagnostic]:
    """ruff over `paths`; with `source`, over that text instead, checked as if it were the one path."""
    overrides = [arg for option in RUFF_OPTIONS for arg in ("--config", option)]
    targets = ["--", *paths] if source is None else [f"--stdin-filename={paths[0]}", "--", "-"]
    try:
        process = await asyncio.create_subprocess_exec(
            find_ruff_bin(), "check", "--isolated", "--no-cache", "--exit-zero", "--output-format", "json",
            "--select", select, *overrides, *targets,
            cwd=root, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
    except OSError as error:  # no runnable ruff binary: a deploy problem, not a reason to lose the review
        raise LintError(f"could not start ruff: {error}") from error
    stdin = source.encode() if source is not None else b""
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=RUFF_TIMEOUT_S)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise LintError(f"ruff timed out after {RUFF_TIMEOUT_S:g}s") from None
    if process.returncode != 0:
        raise LintError(f"ruff failed: {stderr.decode(errors='replace').strip()[:500]}")
    try:
        return _DIAGNOSTICS.validate_json(stdout or b"[]")
    except ValidationError as error:  # a ruff release changed its output format
        raise LintError(f"unexpected ruff output: {error}") from error
