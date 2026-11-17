"""The security lens: Trail of Bits references chosen by language, its own verifier, and the same gate."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoreviewer.github import PullRequestState
from autoreviewer.review.agents import lens_pair, security_references, simplify_agent, simplify_checker_agent
from autoreviewer.review.job import Lens, Reviewed, repo_of, review_and_post, review_change
from autoreviewer.review.memory import ReviewMemory, conversation_id
from autoreviewer.review.pipeline import review_lens
from autoreviewer.review.progress import Progress
from autoreviewer.review.simplify import Simplifier
from autoreviewer.review.workspace import Workspace
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import FakeGitHub, GitRepo, LocalSource, add_commit, agents, finding, job_deps, lens_agents
from tests.test_review import pr_text

pytestmark = pytest.mark.anyio

INJECTION = finding(5, title="User input reaches a shell")


def security_lens(
    findings: list[dict[str, Any]],
    confirm: Callable[[dict[str, Any]], bool] = lambda f: True,
    *,
    prompts: list[str] | None = None,
    crash: bool = False,
) -> tuple[Lens, ...]:
    agents_ = lens_agents(findings, confirm, prompts=prompts, crash=crash)
    return (Lens("security", agents_, instructions=security_references),)


def pull(repo: GitRepo) -> PullRequestUpdated:
    return PullRequestUpdated(installation_id=1, owner="mpfaffenberger", repo="demo", number=7, head_sha=repo.head)


def test_references_follow_the_languages_a_pr_changes() -> None:
    python = security_references(["src/app.py", "README.md"])
    assert "Eval, Exec, and Code Execution" in python and "Fail-Open Security Switches" in python
    typescript = security_references(["web/App.TSX"])
    assert "Background for languages in this PR: javascript." in typescript
    assert "Eval, Exec, and Code Execution" not in typescript
    assert "none recognised" in security_references(["notes.md"])
    assert "Fail-Open Security Switches" in security_references([])  # insecure defaults always apply


async def test_a_confirmed_vulnerability_blocks_as_a_security_comment(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo)
    deps = replace(job_deps(github, tmp_path, []), lenses=security_lens([INJECTION]))
    await review_and_post(deps, pull(repo))
    [review] = github.reviews
    assert review["event"] == "REQUEST_CHANGES"
    [comment] = review["comments"]
    assert comment.body.startswith("**[P1] User input reaches a shell** (security)")


async def test_the_security_verifier_has_the_last_word(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo)
    deps = replace(job_deps(github, tmp_path, []), lenses=security_lens([INJECTION], confirm=lambda f: False))
    await review_and_post(deps, pull(repo))
    assert [r["event"] for r in github.reviews] == ["APPROVE"]


async def test_one_line_reported_by_both_lenses_keeps_the_more_urgent(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo)
    deps = replace(job_deps(github, tmp_path, [finding(5, priority=2)]), lenses=security_lens([INJECTION]))
    await review_and_post(deps, pull(repo))
    [comment] = github.reviews[0]["comments"]
    assert comment.body.startswith("**[P1] User input reaches a shell** (security)")


async def test_a_failed_security_review_fails_closed(repo: GitRepo, tmp_path: Path) -> None:
    """Approving without the security check would be a fail-open default: no verdict at all instead."""
    github = FakeGitHub(repo)
    deps = replace(job_deps(github, tmp_path, []), lenses=security_lens([], crash=True))
    result = await review_and_post(deps, pull(repo))
    assert result.title == "Review failed" and github.reviews == []


async def test_security_reviews_the_whole_pr_even_when_correctness_continues(repo: GitRepo, tmp_path: Path) -> None:
    """A vulnerability found on an earlier commit must still block on the next one."""
    memory, prompts, github = ReviewMemory(tmp_path / "steps"), list[str](), FakeGitHub(repo)
    rejected = security_lens([INJECTION], confirm=lambda f: False, prompts=prompts)
    target = repo_of(pull(repo))
    conversation = conversation_id(target, 7)

    async def review_at(head: str) -> None:
        state = PullRequestState(head_sha=head, base_sha=repo.base, state="open", draft=False, title="t", body="")
        result = await review_change(
            reader=github, source=LocalSource(github.repo), agents=agents([]), work_dir=tmp_path, repo=target,
            number=7, state=state, head_sha=head, memory=memory, lens_version="v1", lenses=rejected,
        )  # fmt: skip
        assert isinstance(result, Reviewed) and result.remember is not None
        memory.remember(conversation, result.remember)

    await review_at(repo.head)
    await review_at(add_commit(repo, {"stats.py": "def average(xs):\n    return sum(xs) / len(xs)\n"}))
    assert len(prompts) == 2 and all("notes.py" in prompt for prompt in prompts)  # unchanged since, still reviewed
    prior = memory.last(conversation)
    assert prior is not None and prior.dropped == []  # the security verifier's rejections aren't the lens's memory


async def test_a_failing_lens_cancels_the_others(repo: GitRepo, tmp_path: Path) -> None:
    """No GPU time on a review that has already failed: a sibling left running would finish later."""
    finished: list[str] = []

    async def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(0.3)
        finished.append("simplifier")
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"suggestions": []})])

    def slow_simplifier(ws: Workspace) -> Simplifier:
        return Simplifier(simplify_agent(FunctionModel(slow), ws), simplify_checker_agent(FunctionModel(slow), ws))

    github = FakeGitHub(repo)
    deps = replace(job_deps(github, tmp_path, []), lenses=security_lens([], crash=True), simplifier=slow_simplifier)
    result = await review_and_post(deps, pull(repo))
    await asyncio.sleep(0.6)  # long enough for an orphaned simplifier to finish
    assert result.title == "Review failed" and finished == []


async def test_a_lint_finding_on_the_same_line_never_hides_a_vulnerability(repo: GitRepo, tmp_path: Path) -> None:
    head = add_commit(repo, {"calc.py": "def run(expression):\n    return eval(expression)\n"})  # line 1: untyped
    github = FakeGitHub(replace(repo, head=head))
    vulnerability = finding(1, path="calc.py", title="eval of input")
    deps = replace(job_deps(github, tmp_path, []), lenses=security_lens([vulnerability]))
    await review_and_post(deps, PullRequestUpdated(1, "mpfaffenberger", "demo", 7, head))
    bodies = [comment.body for comment in github.reviews[0]["comments"]]
    assert any("(types)" in body for body in bodies) and any("(security)" in body for body in bodies)


async def test_the_security_lens_sees_its_prompt_and_the_references(workspace: Workspace) -> None:
    """Run-level instructions must add to the agent's, not replace them (as a pydantic-ai upgrade could)."""
    seen: list[str | None] = []

    def lens(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append([m.instructions for m in messages if isinstance(m, ModelRequest)][-1])
        report = {"findings": [], "overall_correct": True, "reviewed": []}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, report)])

    agent, verifier = lens_pair("security", FunctionModel(lens), FunctionModel(lens), workspace)
    instructions = security_references(["stats.py"])
    await review_lens(
        await pr_text(workspace), agent, verifier, kind="security", instructions=instructions, progress=Progress()
    )
    [seen_instructions] = seen
    assert seen_instructions is not None
    assert "security vulnerabilities it introduces" in seen_instructions
    assert "Fail-Open Security Switches" in seen_instructions
