"""Seeded-bug cases for measuring the reviewer: mutations of this repo's own code at a pinned commit.

Two shapes:

- **Isolated**: the PR is one small change to the pinned snapshot. Easy: the bug is the whole diff.
- **Buried**: the PR is a real feature commit from this repo's history (`host`), with one bug planted on
  a line that commit added, so the reviewer has to find it among hundreds of legitimate changed lines.
  This is what real reviews look like.

Buggy cases name the line the bug lands on; a finding counts as a hit when it cites that file within a
few lines. Clean cases change no behaviour (or are the host commit untouched): any finding posted there
is a false alarm. Titles read like real PR titles and never hint at the bug.
"""

from __future__ import annotations

from dataclasses import dataclass

CORPUS_COMMIT = "8f980f8"
"""The repository snapshot every case mutates. Pinned so results stay comparable as the reviewer changes."""

HIT_TOLERANCE = 3
"""A finding within this many lines of the seeded line, in the same file, counts as catching it."""


@dataclass(frozen=True)
class Case:
    name: str
    path: str
    old: str
    new: str
    title: str
    anchor: str | None = None
    """Text on the line the bug lands on (default: the first line of `new`); None means a clean case."""
    replace_all: bool = False
    clean: bool = False
    host: str | None = None
    """A real commit to use as the PR (its parent is the base); None means a change to `corpus`."""
    corpus: str = CORPUS_COMMIT
    also: tuple[tuple[str, str], ...] = ()
    """More (old, new) edits in the same file, applied before the main one (e.g. an import the change needs)."""

    @property
    def commits(self) -> tuple[str, str]:
        """(base, head before the mutation)."""
        return (f"{self.host}~1", self.host) if self.host else (self.corpus, self.corpus)

    @property
    def target(self) -> str:
        return self.anchor if self.anchor is not None else self.new.splitlines()[0].strip()


CASES: list[Case] = [
    Case(
        "has_review_or",
        "src/autoreviewer/github.py",
        '== author and r.get("commit_id") == commit_sha',
        '== author or r.get("commit_id") == commit_sha',
        "Simplify the duplicate-review check",
    ),
    Case(
        "allowlist_case",
        "src/autoreviewer/web/routing.py",
        'if repository["full_name"].lower() not in repos:',
        'if repository["full_name"] not in repos:',
        "Drop a redundant normalization in routing",
    ),
    Case(
        "resume_inverted",
        "src/autoreviewer/review/job.py",
        "or prior.head_sha == head_sha:",
        "or prior.head_sha != head_sha:",
        "Tidy the resume conditions",
    ),
    Case(
        "truncate_off_by_one",
        "src/autoreviewer/review/render.py",
        'text[: MAX_TEXT - 1] + "…"',
        'text[:MAX_TEXT] + "…"',
        "Simplify the length cap in sanitize()",
    ),
    Case(
        "negative_offset",
        "src/autoreviewer/review/gittools.py",
        "    offset = max(0, offset)\n",
        "",
        "Remove an unneeded clamp in tool paging",
        anchor="chunk = lines[offset : offset + limit]",
    ),
    Case(
        "rstrip_not_removesuffix",
        "src/autoreviewer/ondemand.py",
        'repo.removesuffix(".git")',
        'repo.rstrip(".git")',
        "Use rstrip for the .git suffix",
    ),
    Case(
        "dedupe_key_lost_args",
        "src/autoreviewer/review/compaction.py",
        'return f"{call.tool_name}:{json.dumps(call.args_as_dict(), sort_keys=True, default=str)}"',
        "return call.tool_name",
        "Cheaper deduplication keys for compaction",
    ),
    Case(
        "dedupe_keeps_medium",
        "src/autoreviewer/review/pipeline.py",
        'key=lambda f: f.severity != "high"',
        'key=lambda f: f.severity == "high"',
        "Clarify the dedupe sort key",
    ),
    Case(
        "hunk_numbering",
        "src/autoreviewer/review/diff.py",
        "            line = int(match.group(1))\n            yield None, raw",
        "            line = int(match.group(1)) - 1\n            yield None, raw",
        "Fix hunk header parsing",
    ),
    Case(
        "kill_without_wait",
        "src/autoreviewer/review/workspace.py",
        "        process.kill()\n        await process.wait()\n",
        "        process.kill()\n",
        "Tidy git timeout handling",
    ),
    Case(
        "clean_rename",
        "src/autoreviewer/review/render.py",
        "    for pattern in _SECRETS:\n        text = pattern.sub(",
        "    for secret in _SECRETS:\n        text = secret.sub(",
        "Clearer variable name in sanitize()",
        clean=True,
    ),
    Case(
        "clean_constant",
        "src/autoreviewer/review/gittools.py",
        "PAGE_LINES",
        "DEFAULT_PAGE_LINES",
        "Rename the paging constant",
        replace_all=True,
        clean=True,
    ),
    Case(
        "clean_equivalent",
        "src/autoreviewer/review/pipeline.py",
        "    if more:\n",
        "    if more > 0:\n",
        "Make count checks explicit",
        replace_all=True,
        clean=True,
    ),
    # --- buried in real PRs from this repo ---------------------------------------------------------
    Case(
        "buried_resume_diverged",
        "src/autoreviewer/review/job.py",
        '.status != "ahead"',
        '.status == "behind"',
        "Continue the last review when a PR gets new commits",
        host="8f980f8",
    ),
    Case(
        "buried_rstrip",
        "src/autoreviewer/ondemand.py",
        'repo.removesuffix(".git")',
        'repo.rstrip(".git")',
        "Review any GitHub PR on demand: autoreviewer-review-pr <url>",
        host="8df6242",
    ),
    Case(
        "buried_dedupe_key",
        "src/autoreviewer/review/compaction.py",
        'return f"{call.tool_name}:{json.dumps(call.args_as_dict(), sort_keys=True, default=str)}"',
        "return call.tool_name",
        "Compact review agents' context with pydantic-ai-harness strategies",
        host="078ad63",
    ),
    Case(
        "buried_kill_no_wait",
        "src/autoreviewer/review/workspace.py",
        "        process.kill()\n        await process.wait()\n",
        "        process.kill()\n",
        "Review through a git workspace instead of a diff pasted into the prompt",
        host="62250a4",
    ),
    Case(
        "buried_forget_wrong_dir",
        "src/autoreviewer/review/memory.py",
        "shutil.rmtree(self._dir(conversation), ignore_errors=True)",
        "shutil.rmtree(self._root / conversation, ignore_errors=True)",
        "Continue the last review when a PR gets new commits",
        host="8f980f8",
    ),
    Case(
        "buried_clean_resume",
        "src/autoreviewer/review/job.py",
        "",
        "",
        "Continue the last review when a PR gets new commits",
        host="8f980f8",
        clean=True,
    ),
    Case(
        "buried_clean_ondemand",
        "src/autoreviewer/ondemand.py",
        "",
        "",
        "Review any GitHub PR on demand: autoreviewer-review-pr <url>",
        host="8df6242",
        clean=True,
    ),
]

CLEAN_CONTROLS = tuple(case for case in CASES if case.clean)
"""Changes with nothing to find: every lens's suite runs them, so a lens that cries wolf shows up."""

CLEAN_CONCAT = Case(
    "clean_concat",
    "src/autoreviewer/web/config.py",
    'return f"{kind}.verifier"',
    'return kind + ".verifier"',
    "Build role names by concatenation",
    clean=True,
    corpus="d284fe8",
)
"""Equivalent, well-tested, and touches no text: a control for the docs and tests lenses (main after #32)."""
