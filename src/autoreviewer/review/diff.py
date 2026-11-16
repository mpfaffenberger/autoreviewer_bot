"""Unified-diff facts the review needs. Pure functions over GitHub's per-file `patch` hunks."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatch

from autoreviewer.github import ChangedFile

MAX_CHANGED_LINES = 60_000
"""Above this, a PR is too big to review well; we say so instead of guessing."""

SKIPPED_PATTERNS = (
    "*.lock", "*-lock.json", "*-lock.yaml", "go.sum", "*.min.js", "*.min.css", "*.map", "*.snap",
    "vendor/*", "*/vendor/*", "node_modules/*", "*/node_modules/*", "third_party/*", "*/third_party/*",
)  # fmt: skip

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class Selection:
    reviewable: list[ChangedFile]
    skipped: list[ChangedFile]
    changed_lines: int


def _new_side_lines(patch: str) -> Iterable[tuple[int | None, str]]:
    """Yield (new-file line number or None for removed lines, raw diff line) for every hunk line."""
    line = 0
    for raw in patch.splitlines():
        if match := _HUNK.match(raw):
            line = int(match.group(1))
            yield None, raw
        elif raw.startswith("-"):
            yield None, raw
        elif raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        else:  # added ("+") or context (" ") line: both exist in the new file
            yield line, raw
            line += 1


def commentable_lines(patch: str) -> frozenset[int]:
    """New-file lines GitHub accepts a RIGHT-side review comment on: those shown in the hunks."""
    return frozenset(number for number, _ in _new_side_lines(patch) if number is not None)


def annotate(patch: str) -> str:
    """The patch with each new-file line number in front, so a model can cite lines precisely."""
    out = []
    for number, raw in _new_side_lines(patch):
        prefix = f"{number:>5} " if number is not None else "      "
        out.append(prefix + raw)
    return "\n".join(out)


def changed_line_count(patch: str) -> int:
    return sum(1 for raw in patch.splitlines() if raw[:1] in "+-" and not raw.startswith(("+++", "---")))


def select(files: list[ChangedFile]) -> Selection:
    """Keep files a reviewer should read: present after the change, with a diff, not generated or vendored."""
    reviewable, skipped = [], []
    for changed in files:
        lowered = changed.path.lower()
        if changed.status == "removed" or not changed.patch or any(fnmatch(lowered, p) for p in SKIPPED_PATTERNS):
            skipped.append(changed)
        else:
            reviewable.append(changed)
    return Selection(reviewable, skipped, sum(changed_line_count(f.patch or "") for f in reviewable))
