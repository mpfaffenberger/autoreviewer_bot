"""One lens for correctness and security: findings carry a category, and code routes them to the right verifier."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoreviewer.review.agents import correctness_agent, lens_pair, security_references, verifier_agent
from autoreviewer.review.pipeline import review
from autoreviewer.review.workspace import Workspace
from autoreviewer.web.app import build_lenses, build_security_verifier
from autoreviewer.web.config import Settings
from tests.review_fakes import finding, lens_model, verifier_model
from tests.test_review import pr_text

pytestmark = pytest.mark.anyio

SECURITY = {**finding(5, title="eval of request data"), "category": "security"}
CORRECTNESS = finding(3, title="off by one")


def recording(name: str, calls: list[str]) -> FunctionModel:
    def verify(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(name)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"confirmed": True, "reason": "checked"})])

    return FunctionModel(verify)


async def test_a_combined_lens_routes_security_findings_to_the_security_verifier(workspace: Workspace) -> None:
    calls: list[str] = []
    lens = correctness_agent(lens_model([SECURITY, CORRECTNESS]), workspace, fused=True)
    security = lens_pair("security", lens_model([]), recording("security", calls), workspace)[1]
    outcome = await review(
        await pr_text(workspace), lens, verifier_agent(recording("correctness", calls), workspace),
        security_verifier=security,
    )  # fmt: skip
    assert sorted(calls) == ["correctness", "security"]
    assert sorted((f.line, f.kind) for f in outcome.posted) == [(3, "correctness"), (5, "security")]


async def test_without_a_security_verifier_the_category_is_ignored(workspace: Workspace) -> None:
    lens = correctness_agent(lens_model([SECURITY]), workspace)
    outcome = await review(await pr_text(workspace), lens, verifier_agent(verifier_model(lambda f: True), workspace))
    assert [f.kind for f in outcome.posted] == ["correctness"]


async def test_the_combined_lens_sees_the_security_addendum_and_references(workspace: Workspace) -> None:
    seen: list[str | None] = []

    def lens(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = [m for m in messages if isinstance(m, ModelRequest)][-1]
        seen.append(request.instructions)
        report: dict[str, Any] = {"findings": [], "overall_correct": True, "reviewed": []}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, report)])

    agent = correctness_agent(FunctionModel(lens), workspace, fused=True)
    security = lens_pair("security", lens_model([]), lens_model([]), workspace)[1]
    await review(
        await pr_text(workspace), agent, verifier_agent(verifier_model(lambda f: True), workspace),
        security_verifier=security, instructions=security_references(["stats.py"]),
    )  # fmt: skip
    [instructions] = seen
    assert (
        instructions is not None and "# Security too" in instructions and "Fail-Open Security Switches" in instructions
    )


@pytest.mark.parametrize(("fused", "kinds", "verifier"), [(True, ["dry"], True), (False, ["security", "dry"], False)])
def test_combined_mode_replaces_the_security_lens(fused: bool, kinds: list[str], verifier: bool) -> None:
    settings = Settings(
        app_id=1, app_slug="s", webhook_secret="w", private_key="k", boodleton_api_key="b", fused_security=fused
    )
    assert [lens.kind for lens in build_lenses(settings)] == kinds
    assert (build_security_verifier(settings) is not None) is verifier
