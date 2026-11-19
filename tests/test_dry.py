"""The DRY lens: advice that never blocks, scoped to what changed when a review continues."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoreviewer.github import PullRequestState
from autoreviewer.review.agents import REPORT_TOOL, lens_pair
from autoreviewer.review.job import Lens, Reviewed, repo_of, review_and_post, review_change
from autoreviewer.review.memory import ReviewMemory, conversation_id
from autoreviewer.review.pipeline import MAX_FINDINGS, review_lens
from autoreviewer.review.progress import Progress
from autoreviewer.review.workspace import Workspace
from autoreviewer.web.app import build_lenses, models_note
from autoreviewer.web.config import Settings
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import FakeGitHub, GitRepo, LocalSource, add_commit, agents, finding, job_deps, lens_agents
from tests.test_review import pr_text

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


async def test_only_the_most_confident_advice_is_checked_and_posted(repo: GitRepo, tmp_path: Path) -> None:
    github, asked = FakeGitHub(repo), list[int]()

    def confirm(reported: dict[str, object]) -> bool:
        asked.append(int(str(reported["confidence"])))
        return True

    reports = [finding(line, title=f"dup {line}", priority=3, confidence=confidence) for line, confidence in
               [(1, 5), (2, 9), (3, 7), (4, 8), (5, 6)]]  # fmt: skip
    dry = (Lens("dry", lens_agents(reports, confirm, kind="dry"), whole_pr=False, max_posted=2),)
    await review_and_post(replace(job_deps(github, tmp_path), lenses=dry), pull(repo))
    assert sorted(asked) == [8, 9]  # the other three never cost a verifier call


async def test_hunches_and_out_of_scope_findings_dont_use_up_the_cap(repo: GitRepo, tmp_path: Path) -> None:
    """From the bot's review of #20: the cap was applied before the free filters."""
    github, asked = FakeGitHub(repo), list[int]()

    def confirm(reported: dict[str, object]) -> bool:
        asked.append(int(str(reported["line"])))
        return True

    reports = [
        finding(1, path="elsewhere.py", priority=2, confidence=10),  # not a changed file
        finding(2, priority=2, confidence=2),  # a hunch
        finding(3, priority=3, confidence=8),
        finding(4, priority=3, confidence=7),
    ]
    dry = (Lens("dry", lens_agents(reports, confirm, kind="dry"), whole_pr=False, max_posted=2),)
    await review_and_post(replace(job_deps(github, tmp_path), lenses=dry), pull(repo))
    assert sorted(asked) == [3, 4]


def test_dry_posts_at_most_five() -> None:
    settings = Settings(app_id=1, app_slug="s", webhook_secret="w", private_key="k", boodleton_api_key="b")
    [dry] = [lens for lens in build_lenses(settings) if lens.kind == "dry"]
    assert dry.max_posted == 5


@pytest.mark.parametrize(("max_posted", "reports_as_it_goes"), [(3, False), (MAX_FINDINGS, True)])
async def test_a_capped_lens_ranks_its_findings_at_the_end(
    workspace: Workspace, max_posted: int, reports_as_it_goes: bool
) -> None:
    """From the bot's review of #20: capping what was reported first kept the first 3, not the best 3."""
    seen: list[tuple[list[str], str | None]] = []

    def lens(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = [m for m in messages if isinstance(m, ModelRequest)][-1]
        seen.append(([tool.name for tool in info.function_tools], request.instructions))
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"findings": [], "overall_correct": True, "reviewed": []})]
        )

    agent, verifier = lens_pair("dry", FunctionModel(lens), FunctionModel(lens), workspace)
    pr = await pr_text(workspace)
    await review_lens(pr, agent, verifier, kind="dry", instructions=None, progress=Progress(), max_posted=max_posted)
    [(tools, instructions)] = seen
    assert (REPORT_TOOL in tools) is reports_as_it_goes
    assert instructions is not None and ("Call `report_finding`" in instructions) is reports_as_it_goes


async def test_a_finding_posted_as_it_was_confirmed_is_counted_once(repo: GitRepo, tmp_path: Path) -> None:
    """From the bot's review of #43: the job adds what the Publisher posted back into the outcome, but only
    what the outcome is missing (an uncapped DRY lens posts as it goes, and reports the finding too)."""
    github = FakeGitHub(repo)
    deps = replace(job_deps(github, tmp_path), lenses=(Lens("dry", lens_agents([finding(5)], kind="dry")),))
    result = await review_and_post(deps, PullRequestUpdated(1, "mpfaffenberger", "demo", 7, repo.head))
    assert len(github.review_comments) == 1 and " 1 advisory (non-blocking) finding(s) posted." in result.summary
