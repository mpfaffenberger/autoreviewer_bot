"""The review agents. Models are parameters, so production uses Boodleton and tests use TestModel."""

from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness import FileSystem
from pydantic_ai_harness.filesystem import READ_ONLY_TOOL_NAMES

from autoreviewer.review.gittools import git_tools
from autoreviewer.review.schemas import LensReport, Verification
from autoreviewer.review.workspace import Workspace

PROMPTS = Path(__file__).parent / "prompts"

LENS_LIMITS = UsageLimits(request_limit=60, total_tokens_limit=1_500_000)
VERIFIER_LIMITS = UsageLimits(request_limit=25, total_tokens_limit=400_000)
"""Hard ceilings per run: a looping model must not hold the shared GPU. The lens explores the diff
through tools now, so it needs more round trips than when the diff was pasted into its prompt."""


def boodleton_model(name: str, *, base_url: str, api_key: str) -> OpenAIChatModel:
    return OpenAIChatModel(name, provider=OpenAIProvider(base_url=base_url, api_key=api_key))


def _read_only_repo(root: Path) -> list[LocalWorkspace[None] | FileSystem[None]]:
    """The checkout, readable and searchable, and nothing else: no writes, no shell, no network."""
    return [LocalWorkspace(root, read_only=True), FileSystem(read_only=True, tools=sorted(READ_ONLY_TOOL_NAMES))]


def _agent[OutputT](
    model: Model, workspace: Workspace, output_type: type[OutputT], prompt: str
) -> Agent[None, OutputT]:
    return Agent(
        model,
        deps_type=type(None),
        output_type=output_type,
        instructions=(PROMPTS / prompt).read_text(),
        capabilities=_read_only_repo(workspace.root),
        toolsets=[git_tools(workspace)],
        retries=3,
    )


def correctness_agent(model: Model, workspace: Workspace) -> Agent[None, LensReport]:
    return _agent(model, workspace, LensReport, "correctness.md")


def verifier_agent(model: Model, workspace: Workspace) -> Agent[None, Verification]:
    return _agent(model, workspace, Verification, "verifier.md")
