"""Shared fakes for the review tests: a real local git repo, a scripted GitHub, scripted models."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoreviewer.github import Conclusion, PullRequestState, Repo, ReviewComment, ReviewEvent, ReviewRejected
from autoreviewer.review.agents import correctness_agent, verifier_agent
from autoreviewer.review.job import ReviewDeps
from autoreviewer.review.schemas import LensReport, Verification
from autoreviewer.review.workspace import Workspace, fetch

BOT = "chaos-emerald-reviewer[bot]"

BASE_SOURCE = "def average(xs):\n    total = 0\n    for x in xs:\n        total += x\n    return total / len(xs)\n"
HEAD_SOURCE = "def average(xs):\n    total = 0\n    for x in xs:\n        total += x\n    return total / len(xs) - 1\n"


@dataclass(frozen=True)
class GitRepo:
    path: Path
    base: str
    head: str

    @property
    def url(self) -> str:
        return f"file://{self.path}"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(path: Path) -> GitRepo:
    """base: stats.py + uv.lock + a symlink. head: the seeded bug, a lockfile bump, a new file."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "uploadpack.allowAnySHA1InWant", "true")  # GitHub serves any SHA; mimic it
    (path / "stats.py").write_text(BASE_SOURCE)
    (path / "uv.lock").write_text("version = 1\n")
    (path / "secrets").symlink_to("/etc/passwd")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    base = _git(path, "rev-parse", "HEAD")
    (path / "stats.py").write_text(HEAD_SOURCE)
    (path / "uv.lock").write_text("version = 2\n")
    (path / "notes.py").write_text("GREETING = 'hi'\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "head")
    return GitRepo(path, base, _git(path, "rev-parse", "HEAD"))


class LocalSource:
    """RepoSource over a local repository: the real fetch() path, no network."""

    def __init__(self, repo: GitRepo) -> None:
        self._repo = repo

    async def checkout(self, repo: Repo, *, head: str, base: str, dest: Path) -> Workspace:
        return await fetch(dest, url=self._repo.url, head=head, base=base)


def finding(line: int = 5, *, path: str = "stats.py", title: str = "Mean is off by one") -> dict[str, Any]:
    return {
        "path": path,
        "line": line,
        "severity": "high",
        "title": title,
        "explanation": "average([2, 4]) returns 2 instead of 3 because of the trailing `- 1`.",
        "suggestion": "Drop the `- 1`.",
    }


def _final(info: AgentInfo, args: dict[str, Any]) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=args)])


def _prompt(messages: list[ModelMessage]) -> str:
    return "".join(
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    )


def lens_model(findings: list[dict[str, Any]]) -> FunctionModel:
    return FunctionModel(lambda messages, info: _final(info, {"findings": findings}))


def verifier_model(confirm: Callable[[dict[str, Any]], bool]) -> FunctionModel:
    def decide(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        reported = json.loads(_prompt(messages).split("```json\n", 1)[1].split("\n```", 1)[0])
        ok = confirm(reported)
        return _final(info, {"confirmed": ok, "reason": "checked the code" if ok else "not a real bug"})

    return FunctionModel(decide)


def agents(
    findings: list[dict[str, Any]], confirm: Callable[[dict[str, Any]], bool] = lambda f: True
) -> Callable[[Workspace], tuple[Agent[None, LensReport], Agent[None, Verification]]]:
    return lambda ws: (correctness_agent(lens_model(findings), ws), verifier_agent(verifier_model(confirm), ws))


@dataclass
class FakeGitHub:
    """Implements PullRequests against a GitRepo; records every write."""

    repo: GitRepo
    head_shas: list[str] = field(default_factory=list[str])
    """Successive heads `get_pull` reports (default: the repo's head); the last one repeats."""
    pr_state: str = "open"
    reject_inline: bool = False
    """Make create_review raise ReviewRejected when it has comments, like GitHub's 422."""
    reviews: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    checks: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    async def get_pull(self, repo: Repo, number: int) -> PullRequestState:
        heads = self.head_shas or [self.repo.head]
        head = heads.pop(0) if len(heads) > 1 else heads[0]
        return PullRequestState(
            head_sha=head, base_sha=self.repo.base, state=self.pr_state, draft=False, title="Add average()", body=""
        )

    async def merge_base(self, repo: Repo, base_sha: str, head_sha: str) -> str:
        return self.repo.base

    async def start_check(self, repo: Repo, head_sha: str) -> int:
        self.checks.append({"status": "in_progress", "head_sha": head_sha})
        return len(self.checks)

    async def finish_check(
        self, repo: Repo, check_id: int, *, conclusion: Conclusion, title: str, summary: str
    ) -> None:
        self.checks[check_id - 1].update(status="completed", conclusion=conclusion, title=title, summary=summary)

    async def create_review(
        self, repo: Repo, number: int, *, commit_sha: str, event: ReviewEvent, body: str, comments: list[ReviewComment]
    ) -> None:
        if self.reject_inline and comments:
            raise ReviewRejected("Unprocessable Entity: line must be part of the diff")
        self.reviews.append(
            {"commit_sha": commit_sha, "event": event, "body": body, "comments": comments, "author": BOT}
        )

    async def has_review(self, repo: Repo, number: int, *, commit_sha: str, author: str) -> bool:
        return any(r["commit_sha"] == commit_sha and r["author"] == author for r in self.reviews)


def job_deps(
    github: FakeGitHub, work_dir: Path, findings: list[dict[str, Any]] | None = None, **kwargs: Any
) -> ReviewDeps:
    return ReviewDeps(
        github=github,
        source=LocalSource(github.repo),
        agents=agents([] if findings is None else findings, **kwargs),
        work_dir=work_dir,
        bot_login=BOT,
        models_note="test models",
    )
