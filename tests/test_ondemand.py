"""On-demand reviews of arbitrary PRs: parsing the reference, the review itself, and read-only GitHub access."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from autoreviewer.github import PublicGitHub, Repo
from autoreviewer.ondemand import PullRef, parse_pr_ref, review_pr
from tests.review_fakes import FakeGitHub, GitRepo, LocalSource, agents, finding

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://github.com/pydantic/pydantic-ai/pull/1234", PullRef("pydantic", "pydantic-ai", 1234)),
        ("https://github.com/pydantic/pydantic-ai/pull/1234/files", PullRef("pydantic", "pydantic-ai", 1234)),
        ("https://github.com/o/r.x/pull/7/commits/abc123?w=1#diff", PullRef("o", "r.x", 7)),
        ("  mpfaffenberger/code_puppy#42 ", PullRef("mpfaffenberger", "code_puppy", 42)),
    ],
)
def test_pr_references_are_parsed(text: str, expected: PullRef) -> None:
    assert parse_pr_ref(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "http://github.com/o/r/pull/1",
        "https://gitlab.com/o/r/pull/1",
        "https://github.com/o/r/issues/1",
        "https://github.com/o/r/pull/",
        "https://github.com/o/r/pull/1; rm -rf /",
        "o/r#1 extra",
        "../r#1",
        "",
    ],
)
def test_anything_else_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="not a GitHub pull request"):
        parse_pr_ref(text)


REF = PullRef("someone", "elsewhere", 7)


async def test_a_buggy_pr_reports_request_changes_and_writes_nothing(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo)
    report = await review_pr(
        REF, reader=github, source=LocalSource(repo), agents=agents([finding(5)]), work_dir=tmp_path
    )

    assert report.would_block and report.head_sha == repo.head
    text = report.render()
    assert "**Verdict: REQUEST CHANGES** (nothing was posted to GitHub)" in text
    assert "## `stats.py:5`" in text and "Mean is off by one" in text
    assert github.reviews == [] and github.checks == []


async def test_a_clean_pr_reports_approve(repo: GitRepo, tmp_path: Path) -> None:
    report = await review_pr(
        REF, reader=FakeGitHub(repo), source=LocalSource(repo), agents=agents([]), work_dir=tmp_path
    )
    assert not report.would_block and "**Verdict: APPROVE**" in report.render()


async def test_dropped_candidates_are_listed_with_the_verifiers_reason(repo: GitRepo, tmp_path: Path) -> None:
    report = await review_pr(
        REF,
        reader=FakeGitHub(repo),
        source=LocalSource(repo),
        agents=agents([finding(5), finding(3, title="Loop is wrong")], confirm=lambda f: f["line"] == 5),
        work_dir=tmp_path,
    )
    assert "1 candidate(s) dropped by verification" in report.render()
    assert "`stats.py:3` Loop is wrong: not a real bug" in report.render()


async def test_too_large_prs_say_so(repo: GitRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("autoreviewer.review.job.MAX_CHANGED_LINES", 1)
    report = await review_pr(
        REF, reader=FakeGitHub(repo), source=LocalSource(repo), agents=agents([]), work_dir=tmp_path
    )
    assert not report.would_block and "**Too large to review**" in report.render()


@pytest.mark.parametrize(("token", "expected_auth"), [(None, None), ("ghp_example", "token ghp_example")])
async def test_public_github_sends_credentials_only_when_given_a_token(
    token: str | None, expected_auth: str | None
) -> None:
    seen: list[str | None] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization"))
        return httpx2.Response(200, json={"merge_base_commit": {"sha": "c" * 40}})

    github = PublicGitHub(token)
    github._github = type(github._github)(
        github._github.auth, async_transport=httpx2.MockTransport(handle)
    )
    assert await github.merge_base(Repo(0, "someone", "elsewhere"), "b" * 40, "a" * 40) == "c" * 40
    assert [value.lower() if value else None for value in seen] == [expected_auth.lower() if expected_auth else None]
