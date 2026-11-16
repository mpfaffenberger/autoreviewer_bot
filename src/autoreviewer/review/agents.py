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

from autoreviewer.review.schemas import LensReport, Verification

PROMPTS = Path(__file__).parent / "prompts"

LENS_LIMITS = UsageLimits(request_limit=40, total_tokens_limit=600_000)
VERIFIER_LIMITS = UsageLimits(request_limit=20, total_tokens_limit=250_000)
"""Hard ceilings per run: a looping model must not hold the shared GPU."""


def boodleton_model(name: str, *, base_url: str, api_key: str) -> OpenAIChatModel:
    return OpenAIChatModel(name, provider=OpenAIProvider(base_url=base_url, api_key=api_key))


def _read_only_repo(root: Path) -> list[LocalWorkspace[None] | FileSystem[None]]:
    """The checkout, readable and searchable, and nothing else: no writes, no shell, no network."""
    return [LocalWorkspace(root, read_only=True), FileSystem(read_only=True, tools=sorted(READ_ONLY_TOOL_NAMES))]


def correctness_agent(model: Model, root: Path) -> Agent[None, LensReport]:
    return Agent(
        model,
        deps_type=type(None),
        output_type=LensReport,
        instructions=(PROMPTS / "correctness.md").read_text(),
        capabilities=_read_only_repo(root),
        retries=3,
    )


def verifier_agent(model: Model, root: Path) -> Agent[None, Verification]:
    return Agent(
        model,
        deps_type=type(None),
        output_type=Verification,
        instructions=(PROMPTS / "verifier.md").read_text(),
        capabilities=_read_only_repo(root),
        retries=3,
    )
