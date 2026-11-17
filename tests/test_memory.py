"""Review memory: a new commit continues the last review; anything else starts fresh."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoreviewer.github import PullRequestState, Repo
from autoreviewer.review.agents import correctness_agent, verifier_agent
from autoreviewer.review.job import review_change
from autoreviewer.review.memory import PriorReview, ReviewMemory, conversation_id
from autoreviewer.review.pipeline import Outcome
from autoreviewer.review.schemas import LensReport, Verification
from autoreviewer.review.workspace import Workspace
from tests.review_fakes import FakeGitHub, GitRepo, LocalSource, add_commit, finding, verifier_model

pytestmark = pytest.mark.anyio

REPO = Repo(1, "mpfaffenberger", "demo")
NUMBER = 7
CONVERSATION = conversation_id(REPO, NUMBER)
FIXED = "def average(xs):\n    total = 0\n    for x in xs:\n        total += x\n    return total / len(xs)\n"


@dataclass
class LensSpy:
    """A scripted lens that records what each run was given."""

    findings: list[dict[str, Any]]
    runs: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def model(self) -> FunctionModel:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            prompts = [
                p.content
                for m in messages
                if isinstance(m, ModelRequest)
                for p in m.parts
                if isinstance(p, UserPromptPart)
            ]
            self.runs.append({
                "prompts": prompts,
                "tools": {t.name for t in info.function_tools},
            })  # fmt: skip
            return ModelResponse(
                parts=[ToolCallPart(tool_name=info.output_tools[0].name, args={"findings": self.findings})]
            )

        return FunctionModel(respond)

    def agents(self, workspace: Workspace) -> tuple[Agent[None, LensReport], Agent[None, Verification]]:
        return correctness_agent(self.model(), workspace), verifier_agent(verifier_model(lambda f: True), workspace)


async def review_at(
    github: FakeGitHub, head: str, spy: LensSpy, memory: ReviewMemory, work: Path, version: str = "v1"
) -> Outcome:
    state = PullRequestState(
        head_sha=head, base_sha=github.repo.base, state="open", draft=False, title="Add average()", body=""
    )
    result = await review_change(
        reader=github, source=LocalSource(github.repo), agents=spy.agents, work_dir=work,
        repo=REPO, number=NUMBER, state=state, head_sha=head, memory=memory, lens_version=version,
    )  # fmt: skip
    assert isinstance(result, Outcome)
    return result


@pytest.fixture
def memory(tmp_path: Path) -> ReviewMemory:
    return ReviewMemory(tmp_path / "steps")


async def test_a_new_commit_continues_the_last_review(repo: GitRepo, memory: ReviewMemory, tmp_path: Path) -> None:
    github, spy = FakeGitHub(repo), LensSpy([finding(5)])
    await review_at(github, repo.head, spy, memory, tmp_path)
    first = spy.runs[-1]
    assert "diff_since_last_review" not in first["tools"]
    assert memory.last(CONVERSATION).findings == [{"path": "stats.py", "line": 5, "title": "Mean is off by one"}]  # type: ignore[union-attr]

    new_head = add_commit(repo, {"stats.py": FIXED})
    spy.findings = []
    await review_at(github, new_head, spy, memory, tmp_path)
    second = spy.runs[-1]

    assert first["prompts"][0] in second["prompts"]  # the previous conversation came along
    latest = second["prompts"][-1]
    assert f"moved from `{repo.head[:7]}`" in latest and f"to `{new_head[:7]}`" in latest
    assert "- stats.py:5 Mean is off by one" in latest  # what it posted last time
    assert "- M stats.py (+1 -1)" in latest  # what changed since
    assert "diff_since_last_review" in second["tools"]
    assert memory.last(CONVERSATION).head_sha == new_head  # type: ignore[union-attr]


async def test_the_tool_shows_only_what_changed_since_the_last_review(
    repo: GitRepo, memory: ReviewMemory, tmp_path: Path
) -> None:
    from autoreviewer.review.gittools import git_tools
    from autoreviewer.review.workspace import fetch

    new_head = add_commit(repo, {"stats.py": FIXED})
    workspace = await fetch(tmp_path / "w", url=repo.url, head=new_head, base=repo.base, previous=repo.head)
    tool = git_tools(workspace).tools["diff_since_last_review"]
    text = await tool.function(path="stats.py")  # type: ignore[misc]
    assert "-    return total / len(xs) - 1" in text and "+    return total / len(xs)" in text
    assert "notes.py" not in await tool.function()  # type: ignore[misc]  # unchanged since: not shown


@pytest.mark.parametrize("why", ["force-push", "another version", "same head again"])
async def test_anything_but_new_commits_starts_fresh(
    repo: GitRepo, memory: ReviewMemory, tmp_path: Path, why: str
) -> None:
    github, spy = FakeGitHub(repo), LensSpy([])
    await review_at(github, repo.head, spy, memory, tmp_path)
    if why == "force-push":
        rewritten = "def average(xs):\n    return sum(xs) / len(xs)\n"
        head = add_commit(repo, {"stats.py": rewritten}, branch_from=repo.base)  # no longer contains the old head
        await review_at(github, head, spy, memory, tmp_path)
    elif why == "another version":
        await review_at(github, add_commit(repo, {"stats.py": FIXED}), spy, memory, tmp_path, version="v2")
    else:
        await review_at(github, repo.head, spy, memory, tmp_path)
    assert len(spy.runs[-1]["prompts"]) == 1 and "New commits" not in spy.runs[-1]["prompts"][0]


async def test_a_moved_merge_base_starts_fresh(repo: GitRepo, memory: ReviewMemory, tmp_path: Path) -> None:
    github, spy = FakeGitHub(repo), LensSpy([])
    await review_at(github, repo.head, spy, memory, tmp_path)
    prior = memory.last(CONVERSATION)
    assert prior is not None
    memory.remember(CONVERSATION, PriorReview(**{**prior.__dict__, "base_sha": "f" * 40}))  # e.g. main merged in
    await review_at(github, add_commit(repo, {"stats.py": FIXED}), spy, memory, tmp_path)
    assert len(spy.runs[-1]["prompts"]) == 1


def test_a_corrupt_index_means_no_prior_review(memory: ReviewMemory, tmp_path: Path) -> None:
    memory.remember(CONVERSATION, PriorReview("r", "h", "b", "v"))
    index = next((tmp_path / "steps").glob("*/index.json"))
    index.write_text("{not json")
    assert memory.last(CONVERSATION) is None


async def test_closing_the_pr_forgets_everything(repo: GitRepo, memory: ReviewMemory, tmp_path: Path) -> None:
    await review_at(FakeGitHub(repo), repo.head, LensSpy([]), memory, tmp_path)
    assert any((tmp_path / "steps").iterdir())
    memory.forget(CONVERSATION)
    assert memory.last(CONVERSATION) is None and not any((tmp_path / "steps").iterdir())


def test_conversation_ids_are_per_pr_and_case_insensitive() -> None:
    assert (
        conversation_id(Repo(1, "Owner", "Repo"), 3)
        == conversation_id(Repo(9, "owner", "repo"), 3)
        == "review:owner/repo#3"
    )
    assert conversation_id(REPO, 1) != conversation_id(REPO, 2)
