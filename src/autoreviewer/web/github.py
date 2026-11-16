"""The only module that writes to GitHub. Models never get here: they return typed output, code posts it."""

from __future__ import annotations

from typing import Literal, Protocol

from githubkit import AppAuthStrategy, GitHub

CHECK_NAME = "autoreviewer"

Conclusion = Literal["success", "failure", "neutral"]


class CheckRuns(Protocol):
    async def create_check_run(
        self,
        *,
        installation_id: int,
        owner: str,
        repo: str,
        head_sha: str,
        conclusion: Conclusion,
        title: str,
        summary: str,
    ) -> None: ...


class GitHubApp:
    """Acts as one installation per call; githubkit mints and refreshes the installation tokens."""

    def __init__(self, github: GitHub[AppAuthStrategy]) -> None:
        self._github = github

    @classmethod
    def from_key(cls, app_id: int, private_key: str) -> GitHubApp:
        return cls(GitHub(AppAuthStrategy(app_id, private_key)))

    def _as_installation(self, installation_id: int) -> GitHub:
        return self._github.with_auth(self._github.auth.as_installation(installation_id))

    async def create_check_run(
        self,
        *,
        installation_id: int,
        owner: str,
        repo: str,
        head_sha: str,
        conclusion: Conclusion,
        title: str,
        summary: str,
    ) -> None:
        # Hold the installation client in a local: githubkit's `rest` namespace only keeps a weak reference,
        # so chaining off a temporary lets CPython collect the client mid-call (found in the M0 live test).
        github = self._as_installation(installation_id)
        await github.rest.checks.async_create(
            owner,
            repo,
            data={
                "name": CHECK_NAME,
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": {"title": title, "summary": summary},
            },
        )
