"""The review agents. Models are parameters, so production uses Boodleton and tests use TestModel."""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic_ai import Agent, AgentRetries, ConcurrencyLimiter
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.models import Model
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness import DetectLoops, FileSystem
from pydantic_ai_harness.filesystem import READ_ONLY_TOOL_NAMES
from pydantic_ai_harness.tool_output_limits import READ_TOOL_NAME

from autoreviewer.review.compaction import review_compaction, tool_output_limits
from autoreviewer.review.gittools import git_tools
from autoreviewer.review.schemas import (
    LensKind,
    LensReport,
    SimplifyReport,
    SimplifyVerdict,
    ThreadAnswer,
    Verification,
)
from autoreviewer.review.workspace import Workspace
from autoreviewer.servers import COMPACT_AT, DEEPSEEK, QWEN, REPLY_TOKENS, ReasoningEffort

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
"""No token, tool-call, or cost limits: a lens may explore as much as the change needs. The request limit
only stops a model stuck in a loop; the deadline (deadline.py) bounds its time."""
VERIFIER_LIMITS = UsageLimits(request_limit=80)
"""A verifier checks one finding: needing more than 80 steps means it's stuck. A live security verifier
re-read one file 650+ times, one line further each time, before this. Over budget, the finding stays
unverified (not posted, checked again next review)."""

RETRIES: AgentRetries = {"tools": 25, "output": 3}
"""Consecutive failures allowed per tool before the run fails (any success resets the count).
Exploring means guessing paths: a few misses on a directory that doesn't exist must not end a long
review, as 3 did on PR #5. Twenty-five misses in a row of one tool means the model is stuck."""


def boodleton_model(
    name: str, *, base_url: str, api_key: str, lanes: int | None = None, reasoning: ReasoningEffort | None = None
) -> Model:
    """A model on Boodleton; with `lanes`, at most that many requests to it at once across this process (shared by
    every model object for `name`); with `reasoning`, every request asks for that reasoning effort. Every reply is
    capped at REPLY_TOKENS, which the server keeps room for."""
    settings = OpenAIChatModelSettings(max_tokens=REPLY_TOKENS)
    if reasoning:
        settings["openai_reasoning_effort"] = reasoning
    model = OpenAIChatModel(name, provider=OpenAIProvider(base_url=base_url, api_key=api_key), settings=settings)
    return model if lanes is None else with_lanes(model, name, lanes)


def with_lanes(model: Model, name: str, lanes: int) -> Model:
    """`model` sharing one process-wide limiter with every other model of this name: requests beyond `lanes`
    wait here, rather than in the model server's queue where other users wait too."""
    return ConcurrencyLimitedModel(model, limiter=_lanes(name, lanes))


@functools.cache
def _lanes(name: str, lanes: int) -> ConcurrencyLimiter:
    return ConcurrencyLimiter(max_running=lanes, name=f"{name} lanes")


def _read_only_repo(root: Path) -> list[LocalWorkspace[None] | FileSystem[None]]:
    """The checkout, readable and searchable, and nothing else: no writes, no shell, no network."""
    return [LocalWorkspace(root, read_only=True), FileSystem(read_only=True, tools=sorted(READ_ONLY_TOOL_NAMES))]


def _agent[OutputT](
    model: Model,
    workspace: Workspace,
    output_type: type[OutputT],
    instructions: str,
    compact_at: int,
    *,
    model_settings: ModelSettings | None = None,
) -> Agent[None, OutputT]:
    return Agent(
        model,
        deps_type=type(None),
        output_type=output_type,
        instructions=instructions,
        model_settings=model_settings,
        # DetectLoops nudges a model repeating identical calls, alternating between two, or repeating itself.
        capabilities=[
            *_read_only_repo(workspace.root),
            tool_output_limits(compact_at),
            review_compaction(compact_at),
            DetectLoops(),
        ],
        toolsets=[git_tools(workspace)],
        retries=RETRIES,
    )


REPORT_AS_YOU_GO = """# Reporting

Call `report_finding` the moment you are sure enough of a finding to include it in your report: it is
verified and posted to the PR right away, so the author can start fixing it while you keep reviewing.
Then carry on. Your final report must still list every finding, including those you already reported."""
FOCUS = """# Scope

Review only the change: the lines this pull request adds or removes, and what they affect. Search and read the
rest of the codebase only to understand that change (the surrounding function, its callers, the definitions it
uses, the conventions it should follow). Don't audit code the PR doesn't touch, don't report problems that
existed before it, and don't read files with no bearing on the diff. A focused review finishes sooner, and its
findings are the ones the author can act on."""

LENS_RULES = f"{FOCUS}\n\n{REPORT_AS_YOU_GO}"
"""Run instructions every lens gets, whatever its own prompt says."""

REPORT_TOOL = "report_finding"
"""The tool a lens calls to hand over a finding mid-run (pipeline.Judge)."""


def lens_version(model_name: str, reasoning: ReasoningEffort | None = None) -> str:
    """Fingerprint of what shapes a lens conversation; a saved one is only continued if it matches."""
    material = "\n".join(
        [
            _prompt("correctness.md"),
            model_name,
            f"reasoning={reasoning}",
            json.dumps(LensReport.model_json_schema(), sort_keys=True),
            ",".join(sorted([*READ_ONLY_TOOL_NAMES, REPORT_TOOL, READ_TOOL_NAME])),
            LENS_RULES,
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def correctness_agent(
    model: Model, workspace: Workspace, *, compact_at: int = COMPACT_AT[QWEN]
) -> Agent[None, LensReport]:
    return _agent(model, workspace, LensReport, _prompt("correctness.md"), compact_at)


def verifier_agent(
    model: Model, workspace: Workspace, *, compact_at: int = COMPACT_AT[DEEPSEEK]
) -> Agent[None, Verification]:
    return checker_agent("correctness", model, workspace, compact_at=compact_at)


LENS_PROMPTS: dict[LensKind, tuple[str, str]] = {
    "security": ("security.md", "security_verifier.md"),
    "dry": ("dry.md", "dry_verifier.md"),
    "docs": ("docs.md", "docs_verifier.md"),
    "tests": ("tests.md", "tests_verifier.md"),
}
"""Each extra lens's (lens prompt, verifier prompt)."""

LENS_REFERENCES: dict[str, tuple[Path, ...]] = {"docs": (PSTACK / "skill-unslop.md",)}
"""Vendored standards a lens and its verifier both read, verbatim: the docs lens judges the text a PR writes by
pstack's unslop rules (the verifier confirms only clear breaks of a cited rule)."""


def lens_pair(
    kind: LensKind,
    lens_model: Model,
    verifier_model: Model,
    workspace: Workspace,
    *,
    lens_compact_at: int = COMPACT_AT[QWEN],
    verifier_compact_at: int = COMPACT_AT[DEEPSEEK],
) -> tuple[Agent[None, LensReport], Agent[None, Verification]]:
    """(lens, verifier) for one extra lens. The security lens also wants `security_references` as run
    instructions."""
    lens = _agent(
        lens_model, workspace, LensReport, _prompt(LENS_PROMPTS[kind][0]) + _references(kind), lens_compact_at
    )
    return lens, checker_agent(kind, verifier_model, workspace, compact_at=verifier_compact_at)


def checker_agent(
    kind: Literal["correctness"] | LensKind, model: Model, workspace: Workspace, *, compact_at: int
) -> Agent[None, Verification]:
    """The agent that checks one reported finding of `kind` against the code: the verifier a lens's findings go
    through, and the blind re-check a withdrawal needs (conversation.py)."""
    prompt = "verifier.md" if kind == "correctness" else LENS_PROMPTS[kind][1]
    return _agent(model, workspace, Verification, _prompt(prompt) + _references(kind), compact_at)


def _references(kind: str) -> str:
    return "".join(f"\n{_body(path.read_text())}\n" for path in LENS_REFERENCES.get(kind, ()))


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
    model: Model, workspace: Workspace, *, compact_at: int = COMPACT_AT[QWEN]
) -> Agent[None, SimplifyReport]:
    principles = "\n\n".join(_body(path.read_text()) for path in sorted(PSTACK.glob("principle-*.md")))
    return _agent(model, workspace, SimplifyReport, f"{_prompt('simplify.md')}\n{principles}", compact_at)


def simplify_checker_agent(
    model: Model, workspace: Workspace, *, compact_at: int = COMPACT_AT[DEEPSEEK]
) -> Agent[None, SimplifyVerdict]:
    return _agent(model, workspace, SimplifyVerdict, _prompt("simplify_checker.md"), compact_at)


CONVERSATION_LIMITS = UsageLimits(request_limit=60)
"""An answer reads a few files: plenty, and a runaway thread can't burn the model for long."""


def conversation_agent(
    model: Model,
    workspace: Workspace,
    *,
    compact_at: int = COMPACT_AT[DEEPSEEK],
    thinking: ReasoningEffort | None = None,
) -> Agent[None, ThreadAnswer]:
    """Answers a reply under one of the bot's findings, re-checking the code it's about (see conversation.py)."""
    return _agent(
        model,
        workspace,
        ThreadAnswer,
        _prompt("conversation.md"),
        compact_at,
        model_settings=ModelSettings(thinking=thinking) if thinking else None,
    )


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text()


def _body(skill: str) -> str:
    """A SKILL.md without its YAML front matter."""
    return skill.split("---", 2)[2].strip() if skill.startswith("---") else skill.strip()
