"""The review agents. Models are parameters, so production uses Boodleton and tests use TestModel."""

from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent, AgentRetries
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness import FileSystem
from pydantic_ai_harness.filesystem import READ_ONLY_TOOL_NAMES

from autoreviewer.review.compaction import review_compaction
from autoreviewer.review.gittools import git_tools
from autoreviewer.review.schemas import LensReport, Verification
from autoreviewer.review.workspace import Workspace

PROMPTS = Path(__file__).parent / "prompts"

LENS_LIMITS = UsageLimits(request_limit=10_000)
VERIFIER_LIMITS = UsageLimits(request_limit=10_000)
"""No token, tool-call, or cost limits: a review may explore as much as it needs. The request limit
only stops a model stuck in a loop (pydantic-ai's default is 50). The practical ceiling is the model's
context window per request."""

RETRIES: AgentRetries = {"tools": 25, "output": 3}
"""Consecutive failures allowed per tool before the run fails (any success resets the count).
Exploring means guessing paths: a few misses on a directory that doesn't exist must not end a long
review, as 3 did on PR #5. Twenty-five misses in a row of one tool means the model is stuck."""


def boodleton_model(name: str, *, base_url: str, api_key: str) -> OpenAIChatModel:
    return OpenAIChatModel(name, provider=OpenAIProvider(base_url=base_url, api_key=api_key))


def _read_only_repo(root: Path) -> list[LocalWorkspace[None] | FileSystem[None]]:
    """The checkout, readable and searchable, and nothing else: no writes, no shell, no network."""
    return [LocalWorkspace(root, read_only=True), FileSystem(read_only=True, tools=sorted(READ_ONLY_TOOL_NAMES))]


QWEN_CONTEXT_WINDOW = 262_144
DEEPSEEK_CONTEXT_WINDOW = 1_048_576
"""As reported by the serving engines' /v1/models (max_model_len), 2026-11-16."""


def _agent[OutputT](
    model: Model, workspace: Workspace, output_type: type[OutputT], prompt: str, context_window: int
) -> Agent[None, OutputT]:
    return Agent(
        model,
        deps_type=type(None),
        output_type=output_type,
        instructions=(PROMPTS / prompt).read_text(),
        capabilities=[*_read_only_repo(workspace.root), review_compaction(context_window)],
        toolsets=[git_tools(workspace)],
        retries=RETRIES,
    )


def correctness_agent(
    model: Model, workspace: Workspace, *, context_window: int = QWEN_CONTEXT_WINDOW
) -> Agent[None, LensReport]:
    return _agent(model, workspace, LensReport, "correctness.md", context_window)


def verifier_agent(
    model: Model, workspace: Workspace, *, context_window: int = DEEPSEEK_CONTEXT_WINDOW
) -> Agent[None, Verification]:
    return _agent(model, workspace, Verification, "verifier.md", context_window)
