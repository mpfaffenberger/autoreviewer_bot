"""Shared fakes for the review tests: a scripted GitHub and scripted models (no network anywhere)."""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoreviewer.github import ChangedFile, Conclusion, PullRequestState, Repo, ReviewComment, ReviewEvent
from autoreviewer.review.agents import correctness_agent, verifier_agent
from autoreviewer.review.schemas import LensReport, Verification

PATCH = """@@ -1,5 +1,5 @@
 def average(xs):
     total = 0
     for x in xs:
         total += x
-    return total / len(xs)
+    return total / len(xs) - 1"""

SOURCE = "def average(xs):\n    total = 0\n    for x in xs:\n        total += x\n    return total / len(xs) - 1\n"


def tarball(files: dict[str, str], top: str = "owner-repo-abc1234") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(f"{top}/{path}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


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
) -> Callable[[Path], tuple[Agent[None, LensReport], Agent[None, Verification]]]:
    return lambda root: (correctness_agent(lens_model(findings), root), verifier_agent(verifier_model(confirm), root))


@dataclass
class FakeGitHub:
    """Implements PullRequests; records every write."""

    files: list[ChangedFile] = field(default_factory=lambda: [ChangedFile("stats.py", "modified", PATCH)])
    head_shas: list[str] = field(default_factory=lambda: ["a" * 40])
    """Successive heads `get_pull` reports; the last one repeats."""
    pr_state: str = "open"
    archive: bytes = field(default_factory=lambda: tarball({"stats.py": SOURCE}))
    reviews: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    checks: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    async def get_pull(self, repo: Repo, number: int) -> PullRequestState:
        head = self.head_shas.pop(0) if len(self.head_shas) > 1 else self.head_shas[0]
        return PullRequestState(head_sha=head, state=self.pr_state, draft=False, title="Add average()", body="")

    async def list_files(self, repo: Repo, number: int) -> list[ChangedFile]:
        return self.files

    async def download_tarball(self, repo: Repo, sha: str) -> bytes:
        return self.archive

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
        self.reviews.append({"commit_sha": commit_sha, "event": event, "body": body, "comments": comments})
