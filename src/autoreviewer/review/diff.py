"""Facts about a change, parsed from git's output. Pure functions: no git calls here."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatch

MAX_CHANGED_LINES = 60_000
"""Above this, a PR is too big to review well; we say so instead of guessing."""

SKIPPED_PATTERNS = (
    "*.lock", "*-lock.json", "*-lock.yaml", "go.sum", "*.min.js", "*.min.css", "*.map", "*.snap",
    "vendor/*", "*/vendor/*", "node_modules/*", "*/node_modules/*", "third_party/*", "*/third_party/*",
)  # fmt: skip

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class ChangedPath:
    path: str
    """Path in the head commit (the new name, for renames)."""
    status: str
    """git's status letter: A(dded), M(odified), D(eleted), R(enamed), C(opied), T(ype change)."""
    added: int | None
    removed: int | None
    """None for binary files."""

    @property
    def reviewable(self) -> bool:
        lowered = self.path.lower()
        return self.status != "D" and self.added is not None and not any(fnmatch(lowered, p) for p in SKIPPED_PATTERNS)


@dataclass(frozen=True)
class Selection:
    reviewable: list[ChangedPath]
    skipped: list[ChangedPath]
    changed_lines: int


def parse_changes(name_status_z: str, numstat_z: str) -> list[ChangedPath]:
    """Combine `git diff --name-status -z -M` and `git diff --numstat -z -M` output."""
    statuses: dict[str, str] = {}
    fields = iter(name_status_z.split("\0"))
    for status in fields:
        if not status:
            continue
        if status[0] in "RC":  # rename/copy: old path, then new path
            next(fields)
        statuses[next(fields)] = status[0]

    counts: dict[str, tuple[int | None, int | None]] = {}
    fields = iter(numstat_z.split("\0"))
    for record in fields:
        if not record:
            continue
        added, removed, path = record.split("\t", 2)
        if not path:  # rename: the old and new paths follow as separate fields
            next(fields)
            path = next(fields)
        counts[path] = (None, None) if added == "-" else (int(added), int(removed))

    return [ChangedPath(path, status, *counts.get(path, (None, None))) for path, status in statuses.items()]


def select(changes: list[ChangedPath]) -> Selection:
    reviewable = [c for c in changes if c.reviewable]
    skipped = [c for c in changes if not c.reviewable]
    return Selection(reviewable, skipped, sum((c.added or 0) + (c.removed or 0) for c in reviewable))


def split_git_diff(text: str) -> dict[str, str]:
    """Split a multi-file `git diff` into {new path: hunks}. Deleted and binary files are left out."""
    patches: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = None
        elif current is None and line.startswith("+++ "):
            target = line[4:]
            current = patches.setdefault(target[2:], []) if target.startswith("b/") else None
        elif current is not None:
            current.append(line)
    return {path: "\n".join(lines) for path, lines in patches.items() if lines}


def _new_side_lines(patch: str) -> Iterable[tuple[int | None, str]]:
    """Yield (new-file line number, or None for removed and header lines, raw line) for one file's hunks."""
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


def added_lines(patch: str) -> frozenset[int]:
    """New-file line numbers this patch adds: what a PR can be held responsible for."""
    return frozenset(line for line, raw in _new_side_lines(patch) if line and raw.startswith("+"))


def annotate_git_diff(text: str) -> str:
    """Prefix each line that exists in the new file with its line number, so models cite lines exactly."""
    out: list[str] = []
    line, in_hunk = 0, False
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            in_hunk = False
        elif match := _HUNK.match(raw):
            line, in_hunk = int(match.group(1)), True
        elif in_hunk and raw[:1] in ("+", " "):
            out.append(f"{line:>6} {raw}")
            line += 1
            continue
        out.append(f"       {raw}")
    return "\n".join(out)
