"""The real GitHubApp through real githubkit, with GitHub faked at the HTTP layer.

Unit tests fake the PullRequests boundary, which is how a githubkit lifetime bug reached the M0 live
test. This drives the actual auth flow (RS256 app JWT, installation token exchange) and every call a
review makes, so request shapes and response parsing are checked against what GitHub really sends.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from githubkit import AppAuthStrategy, GitHub

from autoreviewer.github import CHECK_NAME, Comparison, GitHubApp, Repo, ReviewComment, ReviewRejected

pytestmark = pytest.mark.anyio

APP_ID = 5786748
REPO = Repo(installation_id=182623847, owner="mpfaffenberger", name="demo")
HEAD = "a" * 40


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def user(login: str = "octocat") -> dict[str, Any]:
    return {
        "login": login, "id": 1, "node_id": "U", "avatar_url": "https://a", "gravatar_id": "", "url": "https://a",
        "html_url": "https://a", "followers_url": "https://a", "following_url": "https://a", "gists_url": "https://a",
        "starred_url": "https://a", "subscriptions_url": "https://a", "organizations_url": "https://a",
        "repos_url": "https://a", "events_url": "https://a", "received_events_url": "https://a", "type": "User",
        "site_admin": False,
    }  # fmt: skip


class FakeGitHub:
    """Just enough of api.github.com for one review, recording every write."""

    def __init__(self, key: rsa.RSAPrivateKey) -> None:
        self._public_key = key.public_key()
        self.writes: list[tuple[str, str, dict[str, Any]]] = []
        self.token_requests: list[dict[str, Any]] = []
        self.reject_reviews = False

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        path, method = request.url.path, request.method
        if method == "POST" and path == f"/app/installations/{REPO.installation_id}/access_tokens":
            claims = jwt.decode(credential, self._public_key, algorithms=["RS256"])  # raises unless signed by our key
            assert scheme.lower() == "bearer" and str(claims["iss"]) == str(APP_ID)
            if request.content:
                self.token_requests.append(json.loads(request.content))
            expires = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")  # GitHub's exact format
            body = {
                "token": "ghs_installation",
                "expires_at": expires,
                "permissions": {},
                "repository_selection": "selected",
            }
            return httpx2.Response(201, json=body)
        assert credential == "ghs_installation", f"{method} {path} must use the installation token, not the app JWT"
        base = f"/repos/{REPO.owner}/{REPO.name}"
        if method == "GET" and path == f"{base}/compare/{'b' * 40}...{HEAD}":
            assert request.url.params.get("per_page") == "1"
            return httpx2.Response(200, json={"merge_base_commit": {"sha": "c" * 40}, "status": "ahead", "files": []})
        if method == "POST" and path == f"{base}/check-runs":
            self.writes.append(("start_check", path, json.loads(request.content)))
            return httpx2.Response(201, json={"id": 99, "name": CHECK_NAME})
        if method == "PATCH" and path == f"{base}/check-runs/99":
            self.writes.append(("finish_check", path, json.loads(request.content)))
            return httpx2.Response(200, json={"id": 99, "name": CHECK_NAME})
        if method == "POST" and path == f"{base}/pulls/7/reviews":
            if self.reject_reviews:
                return httpx2.Response(
                    422, json={"message": "Unprocessable Entity", "errors": ["line must be part of the diff"]}
                )
            self.writes.append(("create_review", path, json.loads(request.content)))
            return httpx2.Response(200, json={"id": 1})
        return httpx2.Response(404, json={"message": f"unexpected {method} {path}"})


@pytest.fixture
def fake_and_app(key: rsa.RSAPrivateKey) -> tuple[FakeGitHub, GitHubApp]:
    fake = FakeGitHub(key)
    # githubkit speaks httpx2 (its httpx fork), so the fake transport must be an httpx2 one.
    client = GitHub(AppAuthStrategy(APP_ID, pem(key)), async_transport=httpx2.MockTransport(fake.handle))
    return fake, GitHubApp(client)


async def test_compare_reads_merge_base_and_status_from_a_small_compare(
    fake_and_app: tuple[FakeGitHub, GitHubApp],
) -> None:
    _, app = fake_and_app
    assert await app.compare(REPO, "b" * 40, HEAD) == Comparison(merge_base="c" * 40, status="ahead")


async def test_read_tokens_are_scoped_to_one_repo_and_read_only(fake_and_app: tuple[FakeGitHub, GitHubApp]) -> None:
    fake, app = fake_and_app
    assert await app.read_token(REPO) == "ghs_installation"
    assert fake.token_requests == [{"repositories": ["demo"], "permissions": {"contents": "read"}}]


async def test_a_422_review_raises_review_rejected(fake_and_app: tuple[FakeGitHub, GitHubApp]) -> None:
    fake, app = fake_and_app
    fake.reject_reviews = True
    with pytest.raises(ReviewRejected):
        await app.create_review(
            REPO, 7, commit_sha=HEAD, event="COMMENT", body="b", comments=[ReviewComment("x", 1, "y")]
        )


async def test_check_run_lifecycle_and_review_payloads(fake_and_app: tuple[FakeGitHub, GitHubApp]) -> None:
    fake, app = fake_and_app
    first = await app.start_check(REPO, HEAD)
    check_id = await app.start_check(REPO, HEAD)  # twice: the client must survive across calls (the M0 GC bug)
    assert first == check_id
    await app.update_check(REPO, check_id, title="Reviewing", summary="3 tool calls", text="```text\nlog\n```")
    await app.finish_check(REPO, check_id, conclusion="failure", title="1 issue", summary="see review", text="trail")
    suggestion = ReviewComment("f1.py", 7, "```suggestion\nx\n```", start_line=4)
    await app.create_review(
        REPO,
        7,
        commit_sha=HEAD,
        event="REQUEST_CHANGES",
        body="body",
        comments=[ReviewComment("f1.py", 1, "bug"), suggestion],
    )

    assert check_id == 99
    assert fake.writes[0][2] == {"name": CHECK_NAME, "head_sha": HEAD, "status": "in_progress"}
    # Progress leaves the status alone (still in_progress); finishing completes it, keeping the trail.
    assert fake.writes[2][2] == {
        "output": {"title": "Reviewing", "summary": "3 tool calls", "text": "```text\nlog\n```"}
    }
    assert fake.writes[3][2] == {
        "status": "completed",
        "conclusion": "failure",
        "output": {"title": "1 issue", "summary": "see review", "text": "trail"},
    }
    assert fake.writes[4][2] == {
        "commit_id": HEAD,
        "event": "REQUEST_CHANGES",
        "body": "body",
        "comments": [
            {"path": "f1.py", "line": 1, "side": "RIGHT", "body": "bug"},
            {  # a multi-line suggestion: GitHub's one-click "apply" replaces lines 4-7
                "path": "f1.py",
                "line": 7,
                "side": "RIGHT",
                "body": "```suggestion\nx\n```",
                "start_line": 4,
                "start_side": "RIGHT",
            },
        ],
    }
