"""The real GitHubApp through real githubkit, with GitHub faked at the HTTP layer.

Unit tests fake the whole CheckRuns boundary, which is how a githubkit lifetime bug reached the live
test. This drives the actual auth flow: an RS256 app JWT, the installation token exchange, then the call.
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

from autoreviewer.web.github import CHECK_NAME, GitHubApp

pytestmark = pytest.mark.anyio

APP_ID = 5786748
INSTALLATION_ID = 182623847


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


class FakeGitHub:
    """Just enough of api.github.com: the installation token exchange and check-run creation."""

    def __init__(self, key: rsa.RSAPrivateKey) -> None:
        self._public_key = key.public_key()
        self.check_runs: list[dict[str, Any]] = []
        self.token_requests = 0

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        if request.method == "POST" and request.url.path == f"/app/installations/{INSTALLATION_ID}/access_tokens":
            claims = jwt.decode(credential, self._public_key, algorithms=["RS256"])  # raises if not signed by our key
            assert scheme.lower() == "bearer" and str(claims["iss"]) == str(APP_ID)
            self.token_requests += 1
            expires = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")  # GitHub's exact format
            body = {
                "token": "ghs_installation",
                "expires_at": expires,
                "permissions": {},
                "repository_selection": "selected",
            }
            return httpx2.Response(201, json=body)
        if request.method == "POST" and request.url.path == "/repos/mpfaffenberger/demo/check-runs":
            assert credential == "ghs_installation", "check runs must use the installation token, not the app JWT"
            self.check_runs.append(json.loads(request.content))
            return httpx2.Response(201, json={"id": 1, "name": CHECK_NAME})
        return httpx2.Response(404, json={"message": f"unexpected {request.method} {request.url.path}"})


async def test_creates_a_check_run_as_the_installation(key: rsa.RSAPrivateKey) -> None:
    github = FakeGitHub(key)
    # githubkit speaks httpx2 (its httpx fork), so the fake transport must be an httpx2 one.
    client = GitHub(AppAuthStrategy(APP_ID, pem(key)), async_transport=httpx2.MockTransport(github.handle))
    app = GitHubApp(client)

    for sha in ("a" * 40, "b" * 40):  # twice: the client must survive across calls, too
        await app.create_check_run(
            installation_id=INSTALLATION_ID,
            owner="mpfaffenberger",
            repo="demo",
            head_sha=sha,
            conclusion="neutral",
            title="hello",
            summary="world",
        )

    assert [run["head_sha"] for run in github.check_runs] == ["a" * 40, "b" * 40]
    assert github.check_runs[0] == {
        "name": CHECK_NAME,
        "head_sha": "a" * 40,
        "status": "completed",
        "conclusion": "neutral",
        "output": {"title": "hello", "summary": "world"},
    }
