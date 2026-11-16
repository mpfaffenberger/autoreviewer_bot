"""The only module that talks to GitHub. Models never get here: they return typed output, code posts it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from githubkit import AppAuthStrategy, GitHub
from githubkit.exception import RequestFailed

CHECK_NAME = "autoreviewer"

Conclusion = Literal["success", "failure", "neutral"]
ReviewEvent = Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]


@dataclass(frozen=True)
class Repo:
    installation_id: int
    owner: str
    name: str

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}.git"


@dataclass(frozen=True)
class PullRequestState:
    head_sha: str
    base_sha: str
    state: str
    draft: bool
    title: str
    body: str


@dataclass(frozen=True)
class ReviewComment:
    path: str
    line: int
    body: str


class ReviewRejected(Exception):
    """GitHub refused the review (422), typically because a comment's line isn't in its diff view."""


class PullRequests(Protocol):
    """Everything a review job needs from GitHub's API. Code itself comes from git (see workspace.py)."""

    async def get_pull(self, repo: Repo, number: int) -> PullRequestState: ...

    async def merge_base(self, repo: Repo, base_sha: str, head_sha: str) -> str: ...

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

    async def read_token(self, repo: Repo) -> str:
        """A short-lived token that can only read this one repository's contents (for `git fetch`)."""
        response = await self._github.rest.apps.async_create_installation_access_token(
            repo.installation_id, data={"repositories": [repo.name], "permissions": {"contents": "read"}}
        )
        return str(response.json()["token"])

    async def get_pull(self, repo: Repo, number: int) -> PullRequestState:
        github = self._as_installation(repo.installation_id)
        pull = (await github.rest.pulls.async_get(repo.owner, repo.name, number)).parsed_data
        return PullRequestState(
            head_sha=pull.head.sha,
            base_sha=pull.base.sha,
            state=pull.state,
            draft=bool(pull.draft),
            title=pull.title,
            body=pull.body or "",
        )

    async def merge_base(self, repo: Repo, base_sha: str, head_sha: str) -> str:
        """The commit GitHub diffs a PR against (three-dot), so our diff matches its review view."""
        github = self._as_installation(repo.installation_id)
        # per_page=1 keeps the response small; only merge_base_commit is read.
        response = await github.rest.repos.async_compare_commits(
            repo.owner, repo.name, f"{base_sha}...{head_sha}", per_page=1
        )
        return str(response.json()["merge_base_commit"]["sha"])

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
        try:
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
        except RequestFailed as error:
            if error.response.status_code == 422:
                raise ReviewRejected(str(error)) from error
            raise

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
