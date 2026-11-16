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

from autoreviewer.github import CHECK_NAME, GitHubApp, Repo, ReviewComment

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

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        path, method = request.url.path, request.method
        if method == "POST" and path == f"/app/installations/{REPO.installation_id}/access_tokens":
            claims = jwt.decode(credential, self._public_key, algorithms=["RS256"])  # raises unless signed by our key
            assert scheme.lower() == "bearer" and str(claims["iss"]) == str(APP_ID)
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
        if method == "GET" and path == f"{base}/pulls/7/files":
            page = int(request.url.params.get("page", "1"))
            entries = [
                {"sha": "s", "filename": f"f{page}.py", "status": "modified", "additions": 1, "deletions": 1,
                 "changes": 2, "blob_url": "https://a", "raw_url": "https://a", "contents_url": "https://a",
                 "patch": "@@ -1 +1 @@\n-a\n+b"},
                {"sha": "s", "filename": "logo.png", "status": "added", "additions": 0, "deletions": 0,
                 "changes": 0, "blob_url": "https://a", "raw_url": "https://a", "contents_url": "https://a"},
            ] if page <= 2 else []  # fmt: skip
            headers = (
                {"link": f'<https://api.github.com{base}/pulls/7/files?page={page + 1}>; rel="next"'}
                if page < 2
                else {}
            )
            return httpx2.Response(200, json=entries, headers=headers)
        if method == "GET" and path == f"{base}/tarball/{HEAD}":
            return httpx2.Response(200, content=b"\x1f\x8btarball-bytes")
        if method == "POST" and path == f"{base}/check-runs":
            self.writes.append(("start_check", path, json.loads(request.content)))
            return httpx2.Response(201, json={"id": 99, "name": CHECK_NAME})
        if method == "PATCH" and path == f"{base}/check-runs/99":
            self.writes.append(("finish_check", path, json.loads(request.content)))
            return httpx2.Response(200, json={"id": 99, "name": CHECK_NAME})
        if method == "POST" and path == f"{base}/pulls/7/reviews":
            self.writes.append(("create_review", path, json.loads(request.content)))
            return httpx2.Response(200, json={"id": 1})
        return httpx2.Response(404, json={"message": f"unexpected {method} {path}"})


@pytest.fixture
def fake_and_app(key: rsa.RSAPrivateKey) -> tuple[FakeGitHub, GitHubApp]:
    fake = FakeGitHub(key)
    # githubkit speaks httpx2 (its httpx fork), so the fake transport must be an httpx2 one.
    client = GitHub(AppAuthStrategy(APP_ID, pem(key)), async_transport=httpx2.MockTransport(fake.handle))
    return fake, GitHubApp(client)


async def test_list_files_follows_pagination_and_keeps_missing_patches(
    fake_and_app: tuple[FakeGitHub, GitHubApp],
) -> None:
    _, app = fake_and_app
    files = await app.list_files(REPO, 7)
    assert [(f.path, f.patch is None) for f in files] == [
        ("f1.py", False),
        ("logo.png", True),
        ("f2.py", False),
        ("logo.png", True),
    ]


async def test_download_tarball_returns_the_bytes(fake_and_app: tuple[FakeGitHub, GitHubApp]) -> None:
    _, app = fake_and_app
    assert await app.download_tarball(REPO, HEAD) == b"\x1f\x8btarball-bytes"


async def test_check_run_lifecycle_and_review_payloads(fake_and_app: tuple[FakeGitHub, GitHubApp]) -> None:
    fake, app = fake_and_app
    first = await app.start_check(REPO, HEAD)
    check_id = await app.start_check(REPO, HEAD)  # twice: the client must survive across calls (the M0 GC bug)
    assert first == check_id
    await app.finish_check(REPO, check_id, conclusion="failure", title="1 issue", summary="see review")
    await app.create_review(
        REPO, 7, commit_sha=HEAD, event="REQUEST_CHANGES", body="body", comments=[ReviewComment("f1.py", 1, "bug")]
    )

    assert check_id == 99
    assert fake.writes[0][2] == {"name": CHECK_NAME, "head_sha": HEAD, "status": "in_progress"}
    assert fake.writes[2][2] == {
        "status": "completed", "conclusion": "failure", "output": {"title": "1 issue", "summary": "see review"}
    }  # fmt: skip
    assert fake.writes[3][2] == {
        "commit_id": HEAD,
        "event": "REQUEST_CHANGES",
        "body": "body",
        "comments": [{"path": "f1.py", "line": 1, "side": "RIGHT", "body": "bug"}],
    }
