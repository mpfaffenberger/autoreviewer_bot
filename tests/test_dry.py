"""The DRY lens: advice that never blocks, scoped to what changed when a review continues."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from autoreviewer.github import PullRequestState
from autoreviewer.review.job import Lens, Reviewed, repo_of, review_and_post, review_change
from autoreviewer.review.memory import ReviewMemory, conversation_id
from autoreviewer.web.app import build_lenses, models_note
from autoreviewer.web.config import Settings
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import FakeGitHub, GitRepo, LocalSource, add_commit, agents, finding, job_deps, lens_agents

pytestmark = pytest.mark.anyio

REINVENTED = finding(5, title="Use statistics.mean", priority=1)  # even if the lens over-ranks it


def pull(repo: GitRepo) -> PullRequestUpdated:
    return PullRequestUpdated(installation_id=1, owner="mpfaffenberger", repo="demo", number=7, head_sha=repo.head)


async def test_dry_findings_are_advice_at_any_priority(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo)
    dry = (Lens("dry", lens_agents([REINVENTED], kind="dry"), whole_pr=False),)
    deps = replace(job_deps(github, tmp_path, []), lenses=dry, block_at=3)  # where everything else blocks
    result = await review_and_post(deps, pull(repo))
    [review] = github.reviews
    assert review["event"] == "APPROVE" and result.conclusion == "success"
    assert github.inline_comments[0].body.startswith("**[P1] Use statistics.mean** (dry, non-blocking)")


async def test_a_continued_review_only_looks_at_what_changed(repo: GitRepo, tmp_path: Path) -> None:
    memory, prompts, github = ReviewMemory(tmp_path / "steps"), list[str](), FakeGitHub(repo)
    dry = (Lens("dry", lens_agents([], kind="dry", prompts=prompts), whole_pr=False),)
    target = repo_of(pull(repo))

    async def review_at(head: str) -> None:
        state = PullRequestState(head_sha=head, base_sha=repo.base, state="open", draft=False, title="t", body="")
        result = await review_change(
            reader=github, source=LocalSource(github.repo), agents=agents([]), work_dir=tmp_path, repo=target,
            number=7, state=state, head_sha=head, memory=memory, lens_version="v1", lenses=dry,
        )  # fmt: skip
        assert isinstance(result, Reviewed) and result.remember is not None
        memory.remember(conversation_id(target, 7), result.remember)

    await review_at(repo.head)
    await review_at(add_commit(repo, {"stats.py": "def average(xs):\n    return sum(xs) / len(xs)\n"}))
    first, second = prompts
    assert "notes.py" in first and "stats.py" in first  # the whole PR the first time
    assert "stats.py" in second and "notes.py" not in second  # then only what changed since


@pytest.mark.parametrize(
    ("security", "dry", "kinds", "note"),
    [
        (True, True, ["security", "dry"], "correctness, security and DRY by"),
        (False, True, ["dry"], "correctness and DRY by"),
        (False, False, [], "correctness by"),
    ],
)
def test_settings_choose_the_lenses(security: bool, dry: bool, kinds: list[str], note: str) -> None:
    settings = Settings(
        app_id=1, app_slug="s", webhook_secret="w", private_key="k", boodleton_api_key="b", security=security, dry=dry
    )
    lenses = build_lenses(settings)
    assert [lens.kind for lens in lenses] == kinds
    assert all(lens.whole_pr is (lens.kind == "security") for lens in lenses)
    assert models_note(settings).startswith(note)


async def test_a_failing_dry_lens_costs_only_its_advice(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo)
    dry = (Lens("dry", lens_agents([], kind="dry", crash=True), whole_pr=False),)
    deps = replace(job_deps(github, tmp_path, [finding(5)]), lenses=dry)
    result = await review_and_post(deps, pull(repo))
    assert result.conclusion == "failure" and [r["event"] for r in github.reviews] == ["REQUEST_CHANGES"]
