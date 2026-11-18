"""Type annotations, enforced by ruff on the lines a PR adds: no model involved."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from autoreviewer.review import lint
from autoreviewer.review.diff import ChangedPath, added_lines
from autoreviewer.review.job import review_and_post
from autoreviewer.review.lint import type_findings
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import FakeGitHub, GitRepo, add_commit, job_deps

pytestmark = pytest.mark.anyio

UNTYPED = "def scale(xs, factor):\n    return [x * factor for x in xs]\n"


def new_file(root: Path, name: str, source: str) -> tuple[list[ChangedPath], dict[str, str]]:
    """`name` as a file the PR adds: every line is an added line."""
    (root / name).write_text(source)
    lines = source.splitlines()
    patch = f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)
    return [ChangedPath(name, "A", len(lines), 0)], {name: patch}


def test_added_lines_are_only_the_plus_lines() -> None:
    patch = "@@ -1,3 +1,3 @@\n context\n-old\n+new\n context\n"
    assert added_lines(patch) == {2}


async def test_a_new_untyped_function_blocks(tmp_path: Path) -> None:
    [finding] = await type_findings(tmp_path, *new_file(tmp_path, "scale.py", UNTYPED))
    assert (finding.path, finding.line, finding.priority, finding.kind) == ("scale.py", 1, 1, "types")
    assert finding.title == "Add the missing type annotations"
    assert {"`ANN001`", "`ANN201`"} <= {part.rstrip(")") for part in finding.explanation.split() if "ANN" in part}


@pytest.mark.parametrize(
    ("source", "title"),
    [
        ("from typing import Any\n\n\ndef load(raw: Any) -> int:\n    return 1\n", "Replace `Any` with a precise type"),
        (
            "def load(raw: str) -> int:\n    return raw  # type: ignore\n",
            "Name the error code this `# type: ignore` suppresses",
        ),
    ],
)
async def test_any_and_blanket_ignores_are_advisory(tmp_path: Path, source: str, title: str) -> None:
    [finding] = await type_findings(tmp_path, *new_file(tmp_path, "load.py", source))
    assert (finding.priority, finding.title) == (2, title)


async def test_a_typed_function_and_init_without_return_type_pass(tmp_path: Path) -> None:
    source = "class Box:\n    def __init__(self, size: int):\n        self.size = size\n"
    assert await type_findings(tmp_path, *new_file(tmp_path, "box.py", source)) == []


async def test_untouched_lines_are_never_blamed(tmp_path: Path) -> None:
    (tmp_path / "scale.py").write_text(UNTYPED + "\nLIMIT = 3\n")
    patch = "@@ -1,2 +1,4 @@\n def scale(xs, factor):\n     return [x * factor for x in xs]\n+\n+LIMIT = 3\n"
    assert await type_findings(tmp_path, [ChangedPath("scale.py", "M", 2, 0)], {"scale.py": patch}) == []


async def test_the_prs_own_ruff_config_cannot_switch_the_rules_off(tmp_path: Path) -> None:
    # per-file-ignores, unlike `ignore`, would beat our --select: only --isolated stops it.
    (tmp_path / "pyproject.toml").write_text('[tool.ruff.lint.per-file-ignores]\n"*.py" = ["ANN"]\n')
    assert len(await type_findings(tmp_path, *new_file(tmp_path, "scale.py", UNTYPED))) == 1


async def test_non_python_files_are_skipped(tmp_path: Path) -> None:
    assert await type_findings(tmp_path, *new_file(tmp_path, "notes.md", "def f(x): pass\n")) == []


async def test_findings_are_capped_most_urgent_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lint, "MAX_TYPE_FINDINGS", 2)
    source = "from typing import Any\n\n\ndef a(x: Any) -> int: ...\ndef b(x): ...\ndef c(x): ...\n"
    findings = await type_findings(tmp_path, *new_file(tmp_path, "many.py", source))
    assert [(f.line, f.priority) for f in findings] == [(5, 1), (6, 1)]


async def test_a_pr_adding_untyped_code_gets_a_blocking_types_comment(repo: GitRepo, tmp_path: Path) -> None:
    head = add_commit(repo, {"scale.py": UNTYPED})
    github = FakeGitHub(replace(repo, head=head))
    deps = job_deps(github, tmp_path, [])  # the lens finds nothing: the linter alone blocks
    await review_and_post(deps, PullRequestUpdated(1, "mpfaffenberger", "demo", 7, head))
    [review] = github.reviews
    assert review["event"] == "REQUEST_CHANGES"
    [comment] = github.inline_comments
    assert (comment.path, comment.line) == ("scale.py", 1)
    assert comment.body.startswith("**[P1] Add the missing type annotations** (types)")


async def test_broken_python_is_not_a_typing_finding(tmp_path: Path) -> None:
    """ruff reports invalid-syntax whatever is selected; that's the lens's business, not "add annotations"."""
    source = "def scale(xs: list[int]) -> int\n    return len(xs)\n"  # missing colon: an error on line 1
    assert await type_findings(tmp_path, *new_file(tmp_path, "wip.py", source)) == []


async def test_a_broken_linter_skips_the_type_checks_not_the_review(
    repo: GitRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(root: Path, paths: list[str]) -> list[lint.Diagnostic]:
        raise lint.LintError("ruff timed out")

    monkeypatch.setattr(lint, "_ruff", broken)
    head = add_commit(repo, {"scale.py": UNTYPED})
    github = FakeGitHub(replace(repo, head=head))
    await review_and_post(job_deps(github, tmp_path, []), PullRequestUpdated(1, "mpfaffenberger", "demo", 7, head))
    assert [r["event"] for r in github.reviews] == ["APPROVE"]


async def test_a_missing_ruff_binary_is_a_lint_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lint, "find_ruff_bin", lambda: str(tmp_path / "no-such-ruff"))
    with pytest.raises(lint.LintError, match="could not start ruff"):
        await type_findings(tmp_path, *new_file(tmp_path, "scale.py", UNTYPED))
