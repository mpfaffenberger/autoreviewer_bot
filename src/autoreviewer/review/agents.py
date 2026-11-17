"""The review agents. Models are parameters, so production uses Boodleton and tests use TestModel."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

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
from autoreviewer.review.schemas import LensReport, SimplifyReport, SimplifyVerdict, Verification
from autoreviewer.review.workspace import Workspace

PROMPTS = Path(__file__).parent / "prompts"
PSTACK = PROMPTS / "vendor" / "pstack"
TRAIL_OF_BITS = PROMPTS / "vendor" / "trailofbits"

LANGUAGES = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "javascript", ".tsx": "javascript", ".mts": "javascript", ".cts": "javascript",
    ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".cxx": "c", ".hpp": "c", ".hh": "c",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".cs": "csharp", ".php": "php", ".rb": "ruby", ".swift": "swift",
}  # fmt: skip
"""File extension -> Trail of Bits sharp-edges reference (TypeScript shares JavaScript's, C++ shares C's)."""

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
    model: Model, workspace: Workspace, output_type: type[OutputT], instructions: str, context_window: int
) -> Agent[None, OutputT]:
    return Agent(
        model,
        deps_type=type(None),
        output_type=output_type,
        instructions=instructions,
        capabilities=[*_read_only_repo(workspace.root), review_compaction(context_window)],
        toolsets=[git_tools(workspace)],
        retries=RETRIES,
    )


def lens_version(model_name: str) -> str:
    """Fingerprint of what shapes a lens conversation; a saved one is only continued if it matches."""
    material = "\n".join(
        [
            _prompt("correctness.md"),
            model_name,
            json.dumps(LensReport.model_json_schema(), sort_keys=True),
            ",".join(sorted(READ_ONLY_TOOL_NAMES)),
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def correctness_agent(
    model: Model, workspace: Workspace, *, context_window: int = QWEN_CONTEXT_WINDOW
) -> Agent[None, LensReport]:
    return _agent(model, workspace, LensReport, _prompt("correctness.md"), context_window)


def verifier_agent(
    model: Model, workspace: Workspace, *, context_window: int = DEEPSEEK_CONTEXT_WINDOW
) -> Agent[None, Verification]:
    return _agent(model, workspace, Verification, _prompt("verifier.md"), context_window)


def security_agent(
    model: Model, workspace: Workspace, *, context_window: int = QWEN_CONTEXT_WINDOW
) -> Agent[None, LensReport]:
    """Run it with `instructions=security_references(paths)`: the references depend on the PR's languages."""
    return _agent(model, workspace, LensReport, _prompt("security.md"), context_window)


def security_verifier_agent(
    model: Model, workspace: Workspace, *, context_window: int = DEEPSEEK_CONTEXT_WINDOW
) -> Agent[None, Verification]:
    return _agent(model, workspace, Verification, _prompt("security_verifier.md"), context_window)


def security_references(paths: Iterable[str]) -> str:
    """Trail of Bits references for a PR touching `paths`: insecure defaults and auth pitfalls always, plus
    each changed language's sharp edges. Chosen by code from file extensions, so the model can't skip them,
    and read only from this package, never from the repository under review."""
    languages = sorted(
        {LANGUAGES[suffix] for path in paths if (suffix := PurePosixPath(path).suffix.lower()) in LANGUAGES}
    )
    files = [
        *sorted((TRAIL_OF_BITS / "insecure-defaults").glob("*.md")),
        TRAIL_OF_BITS / "sharp-edges" / "auth-patterns.md",
        *(TRAIL_OF_BITS / "sharp-edges" / f"lang-{language}.md" for language in languages),
    ]
    sections = "\n\n".join(file.read_text().strip() for file in files)
    return (
        "# Security references (Trail of Bits skills, CC-BY-SA-4.0, unmodified)\n\n"
        f"Background for languages in this PR: {', '.join(languages) or 'none recognised'}.\n\n{sections}"
    )


def simplify_agent(
    model: Model, workspace: Workspace, *, context_window: int = QWEN_CONTEXT_WINDOW
) -> Agent[None, SimplifyReport]:
    principles = "\n\n".join(_body(path.read_text()) for path in sorted(PSTACK.glob("principle-*.md")))
    return _agent(model, workspace, SimplifyReport, f"{_prompt('simplify.md')}\n{principles}", context_window)


def simplify_checker_agent(
    model: Model, workspace: Workspace, *, context_window: int = DEEPSEEK_CONTEXT_WINDOW
) -> Agent[None, SimplifyVerdict]:
    return _agent(model, workspace, SimplifyVerdict, _prompt("simplify_checker.md"), context_window)


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text()


def _body(skill: str) -> str:
    """A SKILL.md without its YAML front matter."""
    return skill.split("---", 2)[2].strip() if skill.startswith("---") else skill.strip()
