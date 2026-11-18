"""Time limits: a lens past the soft deadline wraps up with a report; a cut-short review never approves."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoreviewer.github import PullRequestState
from autoreviewer.review.agents import REPORT_TOOL, correctness_agent, verifier_agent
from autoreviewer.review.deadline import WRAP_UP, Deadline
from autoreviewer.review.job import Lens, Reviewed, repo_of, review_and_post, review_change
from autoreviewer.review.memory import ReviewMemory, conversation_id
from autoreviewer.review.workspace import Workspace
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import (
    FakeGitHub,
    GitRepo,
    LocalSource,
    agents,
    finding,
    job_deps,
    lens_agents,
    verifier_model,
)

pytestmark = pytest.mark.anyio


async def test_a_lens_past_the_deadline_returns_its_report(workspace: Workspace) -> None:
    """The model would explore forever; once time is up it sees no tools and is told to report."""
    tools_offered: list[int] = []

    def endless(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools_offered.append(len(info.function_tools))
        told = any(
            isinstance(p, UserPromptPart) and p.content == WRAP_UP
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
        )
        if info.function_tools and not told:
            return ModelResponse(parts=[ToolCallPart("changed_files", {})])
        report = {"findings": [finding(5)], "overall_correct": False, "reviewed": ["stats.py"]}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, report)])

    deadline = Deadline(soft_s=0)  # already passed
    run = await correctness_agent(FunctionModel(endless), workspace).run(
        "review", capabilities=[deadline.watch("reviewer")]
    )
    assert tools_offered == [0] and [f.line for f in run.output.findings] == [5]
    assert deadline.wrapped_up == {"reviewer"}


async def test_before_the_deadline_nothing_changes(workspace: Workspace) -> None:
    offered: list[int] = []

    def lens(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.append(len(info.function_tools))
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"findings": [], "overall_correct": True, "reviewed": []})]
        )

    deadline = Deadline(soft_s=3600)
    await correctness_agent(FunctionModel(lens), workspace).run("review", capabilities=[deadline.watch("reviewer")])
    assert offered[0] > 0 and deadline.wrapped_up == set()


def pull(repo: GitRepo) -> PullRequestUpdated:
    return PullRequestUpdated(installation_id=1, owner="mpfaffenberger", repo="demo", number=7, head_sha=repo.head)


async def test_a_review_cut_short_never_approves(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo)
    deps = replace(job_deps(github, tmp_path, []), soft_deadline_s=0, memory=ReviewMemory(tmp_path / "steps"))
    result = await review_and_post(deps, pull(repo))
    assert [r["event"] for r in github.reviews] == ["COMMENT"] and "Partially reviewed" in github.reviews[0]["body"]
    assert (result.conclusion, result.title) == ("neutral", "Partially reviewed")
    assert deps.memory is not None and deps.memory.last(conversation_id(repo_of(pull(repo)), 7)) is None


async def test_a_review_cut_short_still_requests_changes_for_what_it_found(repo: GitRepo, tmp_path: Path) -> None:
    github = FakeGitHub(repo)
    result = await review_and_post(replace(job_deps(github, tmp_path, [finding(5)]), soft_deadline_s=0), pull(repo))
    assert [r["event"] for r in github.reviews] == ["REQUEST_CHANGES"] and result.conclusion == "failure"


async def test_an_advisory_lens_running_out_of_time_doesnt_withhold_approval(repo: GitRepo, tmp_path: Path) -> None:
    deadline = Deadline(soft_s=3600)
    deadline.wrapped_up.add("dry")
    github = FakeGitHub(repo)
    state = PullRequestState(head_sha=repo.head, base_sha=repo.base, state="open", draft=False, title="t", body="")
    result = await review_change(
        reader=github, source=LocalSource(repo), agents=agents([]), work_dir=tmp_path, repo=repo_of(pull(repo)),
        number=7, state=state, head_sha=repo.head, deadline=deadline,
        lenses=(Lens("dry", lens_agents([], kind="dry"), whole_pr=False),),
    )  # fmt: skip
    assert isinstance(result, Reviewed) and not result.outcome.cut_short


async def test_the_hard_deadline_ends_the_review_without_a_verdict(repo: GitRepo, tmp_path: Path) -> None:
    async def stuck(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:  # report one finding at once, then hang
            return ModelResponse(parts=[ToolCallPart(REPORT_TOOL, {"finding": finding(5)})])
        await asyncio.sleep(60)
        raise AssertionError("cancelled long before this")

    github = FakeGitHub(repo)
    deps = replace(
        job_deps(github, tmp_path, []),
        agents=lambda ws: (
            correctness_agent(FunctionModel(stuck), ws),
            verifier_agent(verifier_model(lambda f: True), ws),
        ),
        hard_deadline_s=0.5,
    )
    result = await review_and_post(deps, pull(repo))
    assert result.title == "Review timed out" and github.reviews == []
    assert [c.line for c in github.inline_comments] == [5]  # confirmed before the deadline: still posted
