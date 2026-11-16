"""The review: diff facts, safe checkout, rendering, the lens->verify pipeline, and the job's policies."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from autoreviewer.github import ChangedFile
from autoreviewer.review import checkout
from autoreviewer.review.diff import MAX_CHANGED_LINES, annotate, commentable_lines, select
from autoreviewer.review.job import review_and_post
from autoreviewer.review.pipeline import PullRequestText, review
from autoreviewer.review.render import comment_body, sanitize
from autoreviewer.review.schemas import Finding
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import BOT, PATCH, FakeGitHub, agents, finding, job_deps, tarball

pytestmark = pytest.mark.anyio

HEAD = "a" * 40
PULL = PullRequestUpdated(installation_id=1, owner="mpfaffenberger", repo="demo", number=7, head_sha=HEAD)


# --- diff -----------------------------------------------------------------------------------------


def test_commentable_lines_are_the_new_side_of_the_hunks() -> None:
    patch = "@@ -10,4 +20,5 @@ def f():\n a\n-b\n+c\n+d\n e\n\\ No newline at end of file"
    assert commentable_lines(patch) == frozenset({20, 21, 22, 23})


def test_annotate_numbers_new_lines_and_leaves_removed_lines_blank() -> None:
    lines = annotate(PATCH).splitlines()
    assert lines[1] == "    1  def average(xs):"
    assert lines[5] == "      -    return total / len(xs)"
    assert lines[6] == "    5 +    return total / len(xs) - 1"


def test_select_skips_generated_vendored_removed_and_binary_files() -> None:
    files = [
        ChangedFile("src/app.py", "modified", "@@ -1 +1 @@\n-a\n+b"),
        ChangedFile("uv.lock", "modified", "@@ -1 +1 @@\n-a\n+b"),
        ChangedFile("web/vendor/lib.js", "added", "@@ -0,0 +1 @@\n+x"),
        ChangedFile("old.py", "removed", "@@ -1 +0,0 @@\n-a"),
        ChangedFile("logo.png", "added", None),
    ]
    selection = select(files)
    assert [f.path for f in selection.reviewable] == ["src/app.py"]
    assert selection.changed_lines == 2


# --- checkout -------------------------------------------------------------------------------------


def test_checkout_returns_the_archive_root(tmp_path: Path) -> None:
    root = checkout.extract(tarball({"pkg/mod.py": "x = 1\n"}), tmp_path)
    assert (root / "pkg/mod.py").read_text() == "x = 1\n"


def _one_member_tarball(name: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.addfile(tarfile.TarInfo(name), io.BytesIO(b""))
    return buffer.getvalue()


def test_checkout_refuses_parent_directory_escapes(tmp_path: Path) -> None:
    with pytest.raises(checkout.CheckoutError):
        checkout.extract(_one_member_tarball("../escape.txt"), tmp_path / "dest")
    assert not (tmp_path / "escape.txt").exists()


def test_checkout_confines_absolute_paths(tmp_path: Path) -> None:
    # The data filter strips the leading "/" rather than refusing: the file lands inside the checkout.
    target = f"/tmp/autoreviewer-absolute-{tmp_path.name}"
    root = checkout.extract(_one_member_tarball(target), tmp_path / "dest")
    assert not Path(target).exists()
    assert (root / f"autoreviewer-absolute-{tmp_path.name}").exists()


def test_checkout_refuses_links_out_of_the_tree(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        link = tarfile.TarInfo("top/secrets")
        link.type, link.linkname = tarfile.SYMTYPE, "/home/bazzite/.ssh"
        archive.addfile(link)
    with pytest.raises(checkout.CheckoutError):
        checkout.extract(buffer.getvalue(), tmp_path)


def test_checkout_enforces_size_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkout, "MAX_UNPACKED_BYTES", 10)
    with pytest.raises(checkout.CheckoutError, match="unpacks to more than"):
        checkout.extract(tarball({"big.txt": "x" * 100}), tmp_path)


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


def pr_text() -> PullRequestText:
    return PullRequestText("Add average()", "", [ChangedFile("stats.py", "modified", PATCH)])


async def test_confirmed_findings_on_diff_lines_are_inline(tmp_path: Path) -> None:
    lens, verifier = agents([finding(5)])(tmp_path)
    outcome = await review(pr_text(), lens, verifier)
    assert [(f.path, f.line) for f in outcome.inline] == [("stats.py", 5)]
    assert outcome.general == [] and outcome.blocking


async def test_verifier_rejections_are_dropped(tmp_path: Path) -> None:
    lens, verifier = agents([finding(5), finding(3, title="Loop is wrong")], confirm=lambda f: f["line"] == 5)(tmp_path)
    outcome = await review(pr_text(), lens, verifier)
    assert [f.line for f in outcome.inline] == [5]
    assert [(f.line, reason) for f, reason in outcome.rejected] == [(3, "not a real bug")]


async def test_findings_off_the_diff_go_to_the_body_and_unknown_paths_are_dropped(tmp_path: Path) -> None:
    lens, verifier = agents([finding(40), finding(1, path="never_touched.py")])(tmp_path)
    outcome = await review(pr_text(), lens, verifier)
    assert [f.line for f in outcome.general] == [40]
    assert [reason for _, reason in outcome.rejected] == ["path is not among the changed files"]


async def test_duplicate_findings_are_reported_once(tmp_path: Path) -> None:
    lens, verifier = agents([finding(5), finding(5, title="Same bug, other words")])(tmp_path)
    assert len((await review(pr_text(), lens, verifier)).inline) == 1


# --- job (the review_and_post phase; composition and recovery are in test_durable.py) --------------


async def test_a_bug_requests_changes_with_an_inline_comment(tmp_path: Path) -> None:
    github = FakeGitHub()
    result = await review_and_post(job_deps(github, tmp_path, [finding(5)]), PULL)

    [posted] = github.reviews
    assert posted["event"] == "REQUEST_CHANGES" and posted["commit_sha"] == HEAD
    assert [(c.path, c.line) for c in posted["comments"]] == [("stats.py", 5)]
    assert (result.conclusion, result.title) == ("failure", "1 correctness issue(s)")


async def test_a_clean_pr_is_approved(tmp_path: Path) -> None:
    github = FakeGitHub()
    result = await review_and_post(job_deps(github, tmp_path, []), PULL)
    assert github.reviews[0]["event"] == "APPROVE" and github.reviews[0]["comments"] == []
    assert result.conclusion == "success"


async def test_a_commit_already_reviewed_is_not_reviewed_again(tmp_path: Path) -> None:
    # What a replay looks like after a crash between posting the review and checkpointing the step.
    github = FakeGitHub()
    github.reviews.append({"commit_sha": HEAD, "event": "REQUEST_CHANGES", "body": "", "comments": [], "author": BOT})
    await review_and_post(job_deps(github, tmp_path, [finding(5)]), PULL)
    assert len(github.reviews) == 1


async def test_a_push_during_the_review_supersedes_it(tmp_path: Path) -> None:
    github = FakeGitHub(head_shas=[HEAD, "b" * 40])  # moved on between the start and the post
    result = await review_and_post(job_deps(github, tmp_path, [finding(5)]), PULL)
    assert github.reviews == []
    assert (result.title, result.conclusion) == ("Superseded", "neutral")


async def test_huge_prs_are_not_reviewed(tmp_path: Path) -> None:
    patch = f"@@ -0,0 +1,{MAX_CHANGED_LINES + 1} @@\n" + "\n".join("+x" for _ in range(MAX_CHANGED_LINES + 1))
    github = FakeGitHub(files=[ChangedFile("big.py", "added", patch)])
    result = await review_and_post(job_deps(github, tmp_path, [finding(5)]), PULL)
    assert github.reviews == [] and result.title == "Too large to review"


async def test_a_crash_becomes_a_result_instead_of_an_exception(tmp_path: Path) -> None:
    github = FakeGitHub(archive=b"not a tarball")
    result = await review_and_post(job_deps(github, tmp_path, [finding(5)]), PULL)
    assert github.reviews == [] and (result.title, result.conclusion) == ("Review failed", "neutral")
