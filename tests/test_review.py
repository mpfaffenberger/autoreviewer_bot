"""The review: git-derived diff facts, the checkout, the agents' git tools, the pipeline, and the job's policies."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai import ModelRetry

from autoreviewer.review.diff import (
    ChangedPath,
    annotate_git_diff,
    commentable_lines,
    parse_changes,
    select,
    split_git_diff,
)
from autoreviewer.review.gittools import git_tools
from autoreviewer.review.job import review_and_post
from autoreviewer.review.pipeline import PullRequestText, lens_prompt, review
from autoreviewer.review.render import comment_body, sanitize
from autoreviewer.review.schemas import Finding
from autoreviewer.review.workspace import BASE, HEAD, Workspace, read_changes, run_git
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import BOT, FakeGitHub, GitRepo, agents, finding, job_deps

pytestmark = pytest.mark.anyio


def pull(repo: GitRepo) -> PullRequestUpdated:
    return PullRequestUpdated(installation_id=1, owner="mpfaffenberger", repo="demo", number=7, head_sha=repo.head)


# --- diff facts (pure) ----------------------------------------------------------------------------


def test_commentable_lines_are_the_new_side_of_the_hunks() -> None:
    patch = "@@ -10,4 +20,5 @@ def f():\n a\n-b\n+c\n+d\n e\n\\ No newline at end of file"
    assert commentable_lines(patch) == frozenset({20, 21, 22, 23})


def test_parse_changes_handles_renames_deletions_and_binaries() -> None:
    name_status = "M\0a.py\0R087\0old.py\0new.py\0D\0gone.py\0A\0logo.png\0"
    # Joined with "\0".join, not adjacent literals: a formatter merging "...\0" "2..." yields the octal escape "\02".
    numstat = "\0".join(["3\t1\ta.py", "2\t2\t", "old.py", "new.py", "0\t5\tgone.py", "-\t-\tlogo.png", ""])
    assert parse_changes(name_status, numstat) == [
        ChangedPath("a.py", "M", 3, 1),
        ChangedPath("new.py", "R", 2, 2),
        ChangedPath("gone.py", "D", 0, 5),
        ChangedPath("logo.png", "A", None, None),
    ]


def test_select_skips_generated_vendored_deleted_and_binary_files() -> None:
    changes = [
        ChangedPath("src/app.py", "M", 1, 1),
        ChangedPath("uv.lock", "M", 9, 9),
        ChangedPath("web/vendor/lib.js", "A", 5, 0),
        ChangedPath("old.py", "D", 0, 4),
        ChangedPath("logo.png", "A", None, None),
    ]
    selection = select(changes)
    assert [c.path for c in selection.reviewable] == ["src/app.py"]
    assert selection.changed_lines == 2


def test_split_and_annotate_a_multi_file_git_diff() -> None:
    text = (
        "diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n"
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )
    assert split_git_diff(text) == {"x.py": "@@ -1,2 +1,2 @@\n a\n-b\n+c"}
    annotated = annotate_git_diff(text).splitlines()
    assert annotated[3] == "       +++ b/x.py"  # header lines get no number, even though they start with "-"/"+"
    assert annotated[5] == "     1  a" and annotated[6] == "       -b" and annotated[7] == "     2 +c"


# --- checkout -------------------------------------------------------------------------------------


async def test_checkout_has_head_files_and_named_refs(repo: GitRepo, workspace: Workspace) -> None:
    assert (workspace.root / "stats.py").read_text().endswith("- 1\n")
    assert (await run_git(workspace.root, "rev-parse", BASE)).strip() == repo.base
    assert (await run_git(workspace.root, "rev-parse", HEAD)).strip() == repo.head


async def test_symlinks_are_checked_out_as_plain_files(workspace: Workspace) -> None:
    link = workspace.root / "secrets"
    assert not link.is_symlink() and link.read_text() == "/etc/passwd"  # the target's name, not its contents


async def test_read_changes_lists_files_and_patches(workspace: Workspace) -> None:
    changes, patches = await read_changes(workspace)
    assert {(c.path, c.status) for c in changes} == {("stats.py", "M"), ("uv.lock", "M"), ("notes.py", "A")}
    assert 5 in commentable_lines(patches["stats.py"])


# --- the agents' git tools ------------------------------------------------------------------------


async def call(workspace: Workspace, name: str, **args: object) -> str:
    tool = (git_tools(workspace).tools)[name]
    return await tool.function(**args)  # type: ignore[misc]


async def test_changed_files_marks_what_is_not_reviewed(workspace: Workspace) -> None:
    listing = await call(workspace, "changed_files")
    assert "M +1 -1 stats.py" in listing
    assert "uv.lock  (generated, vendored, binary, or deleted: not reviewed)" in listing


async def test_diff_is_annotated_with_new_line_numbers(workspace: Workspace) -> None:
    text = await call(workspace, "diff", path="stats.py")
    assert "     5 +    return total / len(xs) - 1" in text


async def test_diff_pages_large_output(workspace: Workspace) -> None:
    first = await call(workspace, "diff", limit=3)
    assert first.startswith("[diff (all files): lines 1-3 of") and "call again with offset=3" in first


async def test_show_file_gives_the_version_before_the_pr(workspace: Workspace) -> None:
    text = await call(workspace, "show_file", path="stats.py", ref="base")
    assert "     5      return total / len(xs)" in text and "- 1" not in text
    assert await call(workspace, "show_file", path="notes.py", ref="base") == "notes.py does not exist at base"


@pytest.mark.parametrize("bad", ["/run/secrets/github-app.pem", "../outside.py", "--output=/tmp/x", "a\0b", ""])
async def test_git_tools_refuse_paths_outside_the_repo(workspace: Workspace, bad: str) -> None:
    with pytest.raises(ModelRetry):
        await call(workspace, "diff", path=bad)


# --- render ---------------------------------------------------------------------------------------


def test_sanitize_neutralizes_mentions_but_not_emails_or_code() -> None:
    text = sanitize("ping @mpfaffenberger and @org/team, mail a@b.com, code `@decorator`")
    assert "@\u200bmpfaffenberger" in text and "@\u200borg/team" in text
    assert "a@b.com" in text and "`@decorator`" in text


def test_sanitize_redacts_secrets_and_caps_length() -> None:
    text = sanitize("token ghp_" + "a" * 36 + " key sk_bood_ABCDEFGHIJ " + "x" * 5000)
    assert "ghp_" not in text and "sk_bood_" not in text and text.count("[redacted]") == 2
    assert len(text) <= 3000


def test_comment_body_runs_through_sanitize() -> None:
    body = comment_body(Finding.model_validate({**finding(), "explanation": "cc @someone"}))
    assert "@\u200bsomeone" in body and "(high, correctness)" in body


# --- pipeline -------------------------------------------------------------------------------------


async def pr_text(workspace: Workspace) -> PullRequestText:
    changes, patches = await read_changes(workspace)
    selection = select(changes)
    return PullRequestText("Add average()", "", selection.reviewable, selection.skipped, patches)


async def test_the_lens_prompt_lists_files_but_carries_no_diff(workspace: Workspace) -> None:
    prompt = lens_prompt(await pr_text(workspace))
    assert "- M stats.py (+1 -1)" in prompt and "1 generated, vendored, binary, or deleted" in prompt
    assert "len(xs) - 1" not in prompt  # the model reads the change through its tools


async def test_confirmed_findings_on_diff_lines_are_inline(workspace: Workspace) -> None:
    lens, verifier = agents([finding(5)])(workspace)
    outcome = await review(await pr_text(workspace), lens, verifier)
    assert [(f.path, f.line) for f in outcome.inline] == [("stats.py", 5)]
    assert outcome.general == [] and outcome.blocking


async def test_verifier_rejections_are_dropped(workspace: Workspace) -> None:
    lens, verifier = agents([finding(5), finding(3, title="Loop is wrong")], confirm=lambda f: f["line"] == 5)(
        workspace
    )
    outcome = await review(await pr_text(workspace), lens, verifier)
    assert [f.line for f in outcome.inline] == [5]
    assert [(f.line, reason) for f, reason in outcome.rejected] == [(3, "not a real bug")]


async def test_findings_off_the_diff_go_to_the_body_and_unchanged_paths_are_dropped(workspace: Workspace) -> None:
    lens, verifier = agents([finding(40), finding(1, path="uv.lock"), finding(1, path="never_touched.py")])(workspace)
    outcome = await review(await pr_text(workspace), lens, verifier)
    assert [f.line for f in outcome.general] == [40]
    assert [reason for _, reason in outcome.rejected] == ["path is not among the changed files"] * 2


async def test_duplicate_findings_are_reported_once(workspace: Workspace) -> None:
    lens, verifier = agents([finding(5), finding(5, title="Same bug, other words")])(workspace)
    assert len((await review(await pr_text(workspace), lens, verifier)).inline) == 1


# --- job (the review_and_post phase; composition and recovery are in test_durable.py) --------------


async def test_a_bug_requests_changes_with_an_inline_comment(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo)
    result = await review_and_post(job_deps(github, tmp_path, [finding(5)]), pull(repo))

    [posted] = github.reviews
    assert posted["event"] == "REQUEST_CHANGES" and posted["commit_sha"] == repo.head
    assert [(c.path, c.line) for c in posted["comments"]] == [("stats.py", 5)]
    assert (result.conclusion, result.title) == ("failure", "1 correctness issue(s)")


async def test_a_clean_pr_is_approved(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo)
    result = await review_and_post(job_deps(github, tmp_path, []), pull(repo))
    assert github.reviews[0]["event"] == "APPROVE" and github.reviews[0]["comments"] == []
    assert result.conclusion == "success"


async def test_rejected_inline_comments_fall_back_to_the_body(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo, reject_inline=True)
    result = await review_and_post(job_deps(github, tmp_path, [finding(5)]), pull(repo))
    [posted] = github.reviews
    assert posted["event"] == "REQUEST_CHANGES" and posted["comments"] == []
    assert "`stats.py:5`" in posted["body"] and result.conclusion == "failure"


async def test_a_commit_already_reviewed_is_not_reviewed_again(repo: GitRepo, tmp_path: Path) -> None:
    # What a replay looks like after a crash between posting the review and checkpointing the step.
    github = FakeGitHub(repo)
    github.reviews.append(
        {"commit_sha": repo.head, "event": "REQUEST_CHANGES", "body": "", "comments": [], "author": BOT}
    )
    await review_and_post(job_deps(github, tmp_path, [finding(5)]), pull(repo))
    assert len(github.reviews) == 1


async def test_a_push_during_the_review_supersedes_it(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo, head_shas=[repo.head, "b" * 40])  # moved on between the start and the post
    result = await review_and_post(job_deps(github, tmp_path, [finding(5)]), pull(repo))
    assert github.reviews == []
    assert (result.title, result.conclusion) == ("Superseded", "neutral")


async def test_huge_prs_are_not_reviewed(repo: GitRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("autoreviewer.review.job.MAX_CHANGED_LINES", 1)
    github = FakeGitHub(repo)
    result = await review_and_post(job_deps(github, tmp_path, [finding(5)]), pull(repo))
    assert github.reviews == [] and result.title == "Too large to review"


async def test_a_crash_becomes_a_result_instead_of_an_exception(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo, head_shas=[repo.head])
    deps = job_deps(github, tmp_path, [finding(5)])

    async def broken_compare(*_: object) -> object:
        raise RuntimeError("GitHub is down")

    github.compare = broken_compare  # type: ignore[method-assign]
    result = await review_and_post(deps, pull(repo))
    assert github.reviews == [] and (result.title, result.conclusion) == ("Review failed", "neutral")


# --- tool retries ---------------------------------------------------------------------------------


async def test_a_few_misses_on_missing_paths_do_not_end_the_review(workspace: Workspace) -> None:
    """Regression for PR #5: four list_directory calls on a missing path killed an 8-minute review."""
    from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from autoreviewer.review.agents import LENS_LIMITS, correctness_agent

    misses = iter(range(6))

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if next(misses, None) is not None:
            return ModelResponse(parts=[ToolCallPart(tool_name="list_directory", args={"path": ".cache"})])
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args={"findings": []})])

    result = await correctness_agent(FunctionModel(respond), workspace).run("Review.", usage_limits=LENS_LIMITS)
    assert result.output.findings == []
