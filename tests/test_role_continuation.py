"""Exercise committed role transcripts at the model boundary, not just at the session lookup."""

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoreviewer.review.agents import correctness_agent, simplify_agent, simplify_checker_agent, verifier_agent
from autoreviewer.review.memory import PriorReview
from autoreviewer.review.pipeline import Continuation, review, review_lens
from autoreviewer.review.progress import Progress
from autoreviewer.review.schemas import LensKind
from autoreviewer.review.sessions import RoleSessions
from autoreviewer.review.simplify import Simplifier, suggest
from autoreviewer.review.workspace import Workspace
from tests.review_fakes import finding
from tests.test_simplify import added_file, cut

pytestmark = pytest.mark.anyio


def recorder(output: dict[str, object], calls: list[list[ModelMessage]]) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(messages.copy())
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output.copy())])

    return FunctionModel(respond)


def prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        part.content
        for msg in messages
        if isinstance(msg, ModelRequest)
        for part in msg.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    ]


def sessions(root: Path, head: str = "same-head") -> RoleSessions:
    async def compatible(old: str) -> bool:
        return old == "same-head"

    return RoleSessions(root, head, "base", compatible, "namespace")


async def test_finders_and_checkers_only_resume_their_own_conversations(tmp_path: Path) -> None:
    pr = added_file(tmp_path)
    finds: list[list[ModelMessage]] = []
    checks: list[list[ModelMessage]] = []
    candidate = finding(path="parity.py")
    report: dict[str, object] = dict(findings=[candidate], reviewed=["parity.py"], overall_correct=False)
    workspace = Workspace(tmp_path, "base", "same-head")
    lens = correctness_agent(recorder(report, finds), workspace)
    checker = verifier_agent(recorder(dict(confirmed=True, reason="checked"), checks), workspace)

    async def run(kind: LensKind | None, title: str) -> None:
        store = sessions(tmp_path)
        current = replace(pr, title=title)
        if kind is None:
            outcome = await review(current, lens, checker, sessions=store)
        else:
            outcome = await review_lens(
                current, lens, checker, kind=kind, instructions=None, progress=Progress(), sessions=store
            )
        assert len(outcome.posted) == 1
        store.commit()

    await run(None, "correctness-first")
    await run("security", "security-first")
    assert len(prompts(finds[-1])) == len(prompts(checks[-1])) == 1
    await run(None, "same-head-rerun")
    first_finder, current_finder = prompts(finds[-1])
    first_check, current_check = prompts(checks[-1])
    assert first_finder == prompts(finds[0])[0] and first_check == prompts(checks[0])[0]
    # Not just old user prompts: each model sees its own prior output tool call, and no other role's.
    for messages, expected in ((finds[-1], "findings"), (checks[-1], "confirmed")):
        [prior_response] = [msg for msg in messages if isinstance(msg, ModelResponse)]
        [prior_call] = [part for part in prior_response.parts if isinstance(part, ToolCallPart)]
        assert expected in prior_call.args_as_dict()
    assert "stale" in current_finder and "parity.py" in current_finder
    assert "MUST re-read" in current_check
    await run("security", "security-rerun")
    assert prompts(finds[-1])[:-1] == prompts(finds[1])
    assert prompts(checks[-1])[:-1] == prompts(checks[1])
    # A new hypothesis at the exact same location has no verifier history.
    report["findings"] = [dict(candidate, title="Another defect here")]
    await run("security", "different-candidate")
    assert len(prompts(finds[-1])) == 3 and len(prompts(checks[-1])) == 1

    legacy = Continuation(
        PriorReview("run", "same-head", "base", "version"),
        [ModelRequest(parts=[UserPromptPart("legacy history")])],
        "new-head",
        [],
    )
    await review(pr, lens, checker, continuation=legacy, sessions=sessions(tmp_path, "new-head"))
    old, new = prompts(finds[-1])
    assert old == "legacy history" and "# New commits" in new and "parity.py" in new


async def test_simplifier_resumes_proposal_and_checks_only_the_same_edit(tmp_path: Path) -> None:
    pr = added_file(tmp_path)
    finds: list[list[ModelMessage]] = []
    checks: list[list[ModelMessage]] = []
    proposal: dict[str, object] = dict(suggestions=[cut()])
    workspace = Workspace(tmp_path, "base", "same-head")
    pair = Simplifier(
        simplify_agent(recorder(proposal, finds), workspace),
        simplify_checker_agent(recorder(dict(confirmed=True, reason="checked"), checks), workspace),
    )
    for title in ("first", "again", "different replacement"):
        if title == "different replacement":
            proposal["suggestions"] = [cut(replacement="    return not n % 2")]
        store = sessions(tmp_path, "same-head" if title == "first" else "new-head")
        assert len(await suggest(replace(pr, title=title), pair, tmp_path, Progress(), sessions=store)) == 1
        store.commit()
    assert [len(prompts(turn)) for turn in finds] == [1, 2, 3]
    assert [len(prompts(turn)) for turn in checks] == [1, 2, 1]
    assert prompts(checks[1])[0] == prompts(checks[0])[0] != prompts(finds[0])[0]
    assert "stale" in prompts(finds[1])[-1] and "MUST re-read" in prompts(checks[1])[-1]
