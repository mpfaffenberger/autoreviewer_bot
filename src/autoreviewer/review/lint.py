"""Deterministic checks: rules a linter decides exactly, so no model guesses at them.

Type annotations are enforced with ruff on the lines a PR adds. ruff only parses files (it never runs
them), and `--isolated` ignores the repository's own ruff configuration, so a PR can't switch the rules
off or point ruff at files outside the checkout.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from pathlib import Path
from typing import TypedDict

from pydantic import TypeAdapter
from ruff.__main__ import find_ruff_bin

from autoreviewer.review.diff import ChangedPath, added_lines
from autoreviewer.review.schemas import Finding

logger = logging.getLogger("autoreviewer.lint")

PYTHON_SUFFIXES = (".py", ".pyi")
TYPE_RULES = "ANN0,ANN2,ANN401,PGH003"
"""Missing argument and return annotations, `Any`, and `# type: ignore` without an error code."""
RUFF_OPTIONS = ("lint.flake8-annotations.mypy-init-return = true",)
"""`__init__` with annotated arguments needs no `-> None`, as in mypy."""
ADVISORY_RULES = frozenset({"ANN401", "PGH003"})
"""Sometimes justified (e.g. `Any` at a JSON boundary): advisory, while a missing annotation blocks."""
MAX_TYPE_FINDINGS = 30
RUFF_TIMEOUT_S = 120.0


class LintError(RuntimeError):
    pass


class _Location(TypedDict):
    row: int


class Diagnostic(TypedDict):
    """The part of ruff's JSON output we read."""

    code: str
    message: str
    filename: str
    location: _Location


_DIAGNOSTICS = TypeAdapter(list[Diagnostic])


async def type_findings(root: Path, changes: list[ChangedPath], patches: dict[str, str]) -> list[Finding]:
    """One finding per added line that breaks a typing rule, most urgent first, at most MAX_TYPE_FINDINGS."""
    added = {c.path: added_lines(patches.get(c.path, "")) for c in changes if c.path.endswith(PYTHON_SUFFIXES)}
    if not any(added.values()):
        return []
    by_line: defaultdict[tuple[str, int], list[Diagnostic]] = defaultdict(list)
    for diagnostic in await _ruff(root, sorted(path for path, lines in added.items() if lines)):
        path = Path(diagnostic["filename"]).resolve().relative_to(root.resolve()).as_posix()
        line = diagnostic["location"]["row"]
        if line in added.get(path, ()):
            by_line[(path, line)].append(diagnostic)
    findings = sorted((_finding(path, line, hits) for (path, line), hits in by_line.items()), key=_urgency)
    if len(findings) > MAX_TYPE_FINDINGS:
        logger.info("%d type findings; posting the first %d", len(findings), MAX_TYPE_FINDINGS)
    return findings[:MAX_TYPE_FINDINGS]


def _urgency(finding: Finding) -> tuple[int, str, int]:
    return finding.priority, finding.path, finding.line


def _finding(path: str, line: int, hits: list[Diagnostic]) -> Finding:
    codes = {hit["code"] for hit in hits}
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


async def _ruff(root: Path, paths: list[str]) -> list[Diagnostic]:
    overrides = [arg for option in RUFF_OPTIONS for arg in ("--config", option)]
    process = await asyncio.create_subprocess_exec(
        find_ruff_bin(), "check", "--isolated", "--no-cache", "--exit-zero", "--output-format", "json",
        "--select", TYPE_RULES, *overrides, "--", *paths,
        cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=RUFF_TIMEOUT_S)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise LintError(f"ruff timed out after {RUFF_TIMEOUT_S:g}s") from None
    if process.returncode != 0:
        raise LintError(f"ruff failed: {stderr.decode(errors='replace').strip()[:500]}")
    return _DIAGNOSTICS.validate_json(stdout or b"[]")
