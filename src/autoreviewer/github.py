"""The only module that talks to GitHub. Models never get here: they return typed output, code posts it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from githubkit import AppAuthStrategy, GitHub

CHECK_NAME = "autoreviewer"

Conclusion = Literal["success", "failure", "neutral"]
ReviewEvent = Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]


@dataclass(frozen=True)
class Repo:
    installation_id: int
    owner: str
    name: str


@dataclass(frozen=True)
class PullRequestState:
    head_sha: str
    state: str
    draft: bool
    title: str
    body: str


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str
    patch: str | None
    """Unified diff hunks; GitHub omits it for binary files and very large diffs."""


@dataclass(frozen=True)
class ReviewComment:
    path: str
    line: int
    body: str


class PullRequests(Protocol):
    """Everything a review job needs from GitHub."""

    async def get_pull(self, repo: Repo, number: int) -> PullRequestState: ...

    async def list_files(self, repo: Repo, number: int) -> list[ChangedFile]: ...

    async def download_tarball(self, repo: Repo, sha: str) -> bytes: ...

    async def start_check(self, repo: Repo, head_sha: str) -> int: ...

    async def finish_check(
        self, repo: Repo, check_id: int, *, conclusion: Conclusion, title: str, summary: str
    ) -> None: ...

    async def create_review(
        self, repo: Repo, number: int, *, commit_sha: str, event: ReviewEvent, body: str, comments: list[ReviewComment]
    ) -> None: ...

    async def has_review(self, repo: Repo, number: int, *, commit_sha: str, author: str) -> bool:
        """Whether `author` already reviewed this exact commit (makes posting safe to replay)."""
        ...


class GitHubApp:
    """Acts as one installation per call; githubkit mints and refreshes the installation tokens."""

    def __init__(self, github: GitHub[AppAuthStrategy]) -> None:
        self._github = github

    @classmethod
    def from_key(cls, app_id: int, private_key: str) -> GitHubApp:
        return cls(GitHub(AppAuthStrategy(app_id, private_key)))

    def _as_installation(self, installation_id: int) -> GitHub:
        # Callers must hold the result in a local: githubkit's `rest` namespace only keeps a weak
        # reference, so chaining off this temporary lets CPython collect it mid-call (M0 live test).
        return self._github.with_auth(self._github.auth.as_installation(installation_id))

    async def get_pull(self, repo: Repo, number: int) -> PullRequestState:
        github = self._as_installation(repo.installation_id)
        pull = (await github.rest.pulls.async_get(repo.owner, repo.name, number)).parsed_data
        return PullRequestState(
            head_sha=pull.head.sha, state=pull.state, draft=bool(pull.draft), title=pull.title, body=pull.body or ""
        )

    async def list_files(self, repo: Repo, number: int) -> list[ChangedFile]:
        github = self._as_installation(repo.installation_id)
        files: list[ChangedFile] = []
        async for entry in github.rest.paginate(
            github.rest.pulls.async_list_files, owner=repo.owner, repo=repo.name, pull_number=number, per_page=100
        ):
            patch = entry.patch if isinstance(entry.patch, str) else None
            files.append(ChangedFile(path=entry.filename, status=entry.status, patch=patch))
        return files

    async def download_tarball(self, repo: Repo, sha: str) -> bytes:
        github = self._as_installation(repo.installation_id)
        response = await github.rest.repos.async_download_tarball_archive(repo.owner, repo.name, sha)
        return response.content

    async def start_check(self, repo: Repo, head_sha: str) -> int:
        github = self._as_installation(repo.installation_id)
        response = await github.rest.checks.async_create(
            repo.owner,
            repo.name,
            data={"name": CHECK_NAME, "head_sha": head_sha, "status": "in_progress"},
        )
        # Only the id is needed; validating the whole check-run schema would only add brittleness.
        return int(response.json()["id"])

    async def finish_check(
        self, repo: Repo, check_id: int, *, conclusion: Conclusion, title: str, summary: str
    ) -> None:
        github = self._as_installation(repo.installation_id)
        await github.rest.checks.async_update(
            repo.owner,
            repo.name,
            check_id,
            data={"status": "completed", "conclusion": conclusion, "output": {"title": title, "summary": summary}},
        )

    async def create_review(
        self, repo: Repo, number: int, *, commit_sha: str, event: ReviewEvent, body: str, comments: list[ReviewComment]
    ) -> None:
        github = self._as_installation(repo.installation_id)
        await github.rest.pulls.async_create_review(
            repo.owner,
            repo.name,
            number,
            data={
                "commit_id": commit_sha,
                "event": event,
                "body": body,
                "comments": [{"path": c.path, "line": c.line, "side": "RIGHT", "body": c.body} for c in comments],
            },
        )

    async def has_review(self, repo: Repo, number: int, *, commit_sha: str, author: str) -> bool:
        github = self._as_installation(repo.installation_id)
        page = 1
        while True:
            # Raw JSON, not githubkit's models: only two fields matter and the full schema is large.
            response = await github.rest.pulls.async_list_reviews(
                repo.owner, repo.name, number, per_page=100, page=page
            )
            reviews = response.json()
            if any((r.get("user") or {}).get("login") == author and r.get("commit_id") == commit_sha for r in reviews):
                return True
            if len(reviews) < 100:
                return False
            page += 1
