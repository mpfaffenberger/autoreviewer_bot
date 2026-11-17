"""Optional one-click simplifications: proposed by a model, ruled out by code where provable, checked by
a second model, and posted as GitHub suggestions that never block."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from autoreviewer.review.diff import ChangedPath
from autoreviewer.review.job import review_and_post
from autoreviewer.review.pipeline import PullRequestText
from autoreviewer.review.progress import Progress
from autoreviewer.review.render import comment_body
from autoreviewer.review.schemas import Finding
from autoreviewer.review.simplify import suggest
from autoreviewer.review.workspace import Workspace
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import FakeGitHub, GitRepo, add_commit, job_deps, simplifier

pytestmark = pytest.mark.anyio

BLOATED = """def is_even(n: int) -> bool:
    # check whether n is even
    result = n % 2 == 0
    if result:
        return True
    else:
        return False
"""
TIGHT = "    return n % 2 == 0"


def cut(start: int = 2, end: int = 7, replacement: str = TIGHT, path: str = "parity.py") -> dict[str, Any]:
    return {
        "path": path,
        "start_line": start,
        "end_line": end,
        "replacement": replacement,
        "title": "Return the comparison directly",
        "explanation": "The temporary and the if/else restate a boolean.",
    }


def added_file(root: Path, name: str = "parity.py", source: str = BLOATED, *, first_added: int = 1) -> PullRequestText:
    """`name` in the checkout; lines from `first_added` on are what the PR adds."""
    (root / name).write_text(source)
    lines = source.splitlines()
    context = "".join(f" {line}\n" for line in lines[: first_added - 1])
    added = "".join(f"+{line}\n" for line in lines[first_added - 1 :])
    patch = f"@@ -1,{first_added - 1} +1,{len(lines)} @@\n{context}{added}"
    change = ChangedPath(name, "A", len(lines), 0)
    return PullRequestText("Add is_even", "", [change], [], {name: patch})


async def run(
    tmp_path: Path,
    suggestions: list[dict[str, Any]],
    *,
    pr: PullRequestText | None = None,
    confirm: Callable[[str], bool] = lambda prompt: True,
    crash: bool = False,
) -> tuple[list[Finding], Progress]:
    progress = Progress()
    factory = simplifier(suggestions, confirm, crash=crash)
    found = await suggest(pr or added_file(tmp_path), factory(Workspace(tmp_path, "b", "h")), tmp_path, progress)
    return found, progress


async def test_a_checked_simplification_becomes_a_one_click_suggestion(tmp_path: Path) -> None:
    [finding], _ = await run(tmp_path, [cut()])
    assert (finding.path, finding.start_line, finding.line, finding.kind) == ("parity.py", 2, 7, "simplify")
    assert finding.replacement == TIGHT and finding.lines_cut == 5


@pytest.mark.parametrize(
    ("suggestion", "reason"),
    [
        (cut(start=1), "touches lines this PR didn't add"),
        (cut(replacement=BLOATED.split("\n", 1)[1] + "    # and a note"), "not shorter"),
        (cut(replacement="    return n % 2 =="), "the file no longer parses"),
        (cut(replacement="    return 'ghp_" + "a" * 36 + "'"), "contains something that looks like a secret"),
        (cut(path="elsewhere.py"), "not one of the files in scope"),
        (cut(start=5, end=3), "bad line range"),
    ],
)
async def test_code_rules_out_what_it_can_prove_wrong(tmp_path: Path, suggestion: dict[str, Any], reason: str) -> None:
    pr = added_file(tmp_path, first_added=2)  # line 1 already existed
    found, progress = await run(tmp_path, [suggestion], pr=pr)
    assert found == [] and any(reason in event for event in progress.activity)


async def test_overlapping_suggestions_keep_the_first(tmp_path: Path) -> None:
    found, _ = await run(tmp_path, [cut(), cut(start=3, end=3, replacement="")])
    assert [(f.start_line, f.line) for f in found] == [(2, 7)]


async def test_the_checker_has_the_last_word(tmp_path: Path) -> None:
    found, progress = await run(tmp_path, [cut()], confirm=lambda prompt: False)
    assert found == [] and "dropped simplification: Return the comparison directly" in progress.activity


async def test_a_failing_simplifier_costs_only_the_suggestions(tmp_path: Path) -> None:
    found, _ = await run(tmp_path, [cut()], crash=True)
    assert found == []


async def test_only_files_in_scope_are_offered(tmp_path: Path) -> None:
    pr, prompts = added_file(tmp_path), []
    factory = simplifier([cut()], prompts=prompts)
    assert await suggest(pr, factory(Workspace(tmp_path, "b", "h")), tmp_path, Progress(), paths=set()) == []
    assert prompts == []  # nothing in scope: the model isn't even asked


@pytest.mark.parametrize("code", ["    return n % 2 == 0", ""])
def test_a_suggestion_without_backticks_gets_a_plain_fence(code: str) -> None:
    finding = Finding(
        path="a.py", line=3, start_line=2, replacement=code, priority=3, confidence=10,
        title="t", explanation="e", kind="simplify",
    )  # fmt: skip
    assert comment_body(finding, blocking=False).endswith(f"```suggestion\n{code}\n```")


def test_the_suggestion_block_is_applied_verbatim_and_cannot_be_broken_out_of() -> None:
    code = "    @dataclass\n    class Box: ...  # ```not a fence```"
    finding = Finding(
        path="a.py", line=4, start_line=2, replacement=code, priority=3, confidence=10,
        title="Use a dataclass", explanation="cc @someone", kind="simplify",
    )  # fmt: skip
    body = comment_body(finding, blocking=False)
    assert body.startswith("**Use a dataclass** (simplify, non-blocking)")
    assert "cc @\u200bsomeone" in body  # pinged nowhere outside the code
    assert body.endswith(f"````suggestion\n{code}\n````")  # decorator intact, longer fence


async def test_simplifications_never_block_and_post_as_multi_line_suggestions(repo: GitRepo, tmp_path: Path) -> None:
    head = add_commit(repo, {"parity.py": BLOATED})
    github = FakeGitHub(replace(repo, head=head))
    deps = replace(job_deps(github, tmp_path, []), simplifier=simplifier([cut()]), block_at=3)
    await review_and_post(deps, PullRequestUpdated(1, "mpfaffenberger", "demo", 7, head))
    [review] = github.reviews
    assert review["event"] == "APPROVE"  # even with block_at=3, where every other finding blocks
    [comment] = review["comments"]
    assert (comment.path, comment.start_line, comment.line) == ("parity.py", 2, 7)
    assert "```suggestion\n" + TIGHT + "\n```" in comment.body
    assert (
        "1 optional one-click simplification(s), cutting 5 line(s) (56% of the 9 added)."
    )  # 2 lines elsewhere in review["body"]
