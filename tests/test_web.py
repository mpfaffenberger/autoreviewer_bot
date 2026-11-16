"""The webhook receiver: signature checks, dedupe, routing, and the greeting job."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from autoreviewer.web.app import GREETING_TITLE, create_app
from autoreviewer.web.config import Settings, parse_repos
from autoreviewer.web.dispatch import Dispatcher
from autoreviewer.web.github import Conclusion
from autoreviewer.web.routing import Ignore, MalformedPayload, Pong, PullRequestUpdated, route

pytestmark = pytest.mark.anyio

SECRET = "test-webhook-secret"
REPO = "mpfaffenberger/pydantic-ai-claude-code"
REPOS = frozenset({REPO})


def pull_request_payload(*, action: str = "opened", repo: str = REPO, draft: bool = False) -> dict[str, Any]:
    owner, name = repo.split("/")
    return {
        "action": action,
        "installation": {"id": 182623847},
        "repository": {"full_name": repo, "name": name, "owner": {"login": owner}},
        "pull_request": {"number": 7, "draft": draft, "head": {"sha": "0123456789abcdef0123456789abcdef01234567"}},
    }


def signature(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@dataclass
class FakeChecks:
    created: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

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
        self.created.append(
            dict(
                installation_id=installation_id,
                owner=owner,
                repo=repo,
                head_sha=head_sha,
                conclusion=conclusion,
                title=title,
            )
        )


@dataclass
class Receiver:
    client: httpx.AsyncClient
    checks: FakeChecks
    dispatcher: Dispatcher

    async def deliver(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        delivery: str = "d-1",
        sign: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        body = json.dumps(payload).encode()
        sent = {
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": sign if sign is not None else signature(body),
        }
        return await self.client.post("/webhook", content=body, headers={**sent, **(headers or {})})


@pytest.fixture
async def receiver() -> AsyncIterator[Receiver]:
    settings = Settings(
        app_id=1, app_slug="chaos-emerald-reviewer", webhook_secret=SECRET, private_key="unused", repos=REPOS
    )
    checks, dispatcher = FakeChecks(), Dispatcher()
    app = create_app(settings, checks, dispatcher)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield Receiver(client, checks, dispatcher)


# --- signatures ---------------------------------------------------------------------------------


async def test_ping_with_a_valid_signature_pongs(receiver: Receiver) -> None:
    response = await receiver.deliver("ping", {"zen": "Keep it logically awesome."})
    assert response.status_code == 200
    assert response.json() == {"status": "pong"}


@pytest.mark.parametrize(
    "sign",
    [
        signature(b"different body"),
        "sha256=" + hmac.new(b"wrong-secret", b"{}", hashlib.sha256).hexdigest(),
        "sha1=" + hmac.new(SECRET.encode(), b"{}", hashlib.sha1).hexdigest(),  # downgrade attempt
        "",
    ],
    ids=["other-body", "wrong-secret", "sha1-downgrade", "missing"],
)
async def test_bad_signatures_are_rejected_before_anything_else(receiver: Receiver, sign: str) -> None:
    response = await receiver.deliver("pull_request", pull_request_payload(), sign=sign)
    assert response.status_code == 401
    await receiver.dispatcher.drain()
    assert receiver.checks.created == []


async def test_a_sha1_signature_header_alone_is_not_enough(receiver: Receiver) -> None:
    body = json.dumps({"zen": "x"}).encode()
    sha1 = "sha1=" + hmac.new(SECRET.encode(), body, hashlib.sha1).hexdigest()
    response = await receiver.client.post(
        "/webhook", content=body, headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "d", "X-Hub-Signature": sha1}
    )
    assert response.status_code == 401


# --- delivery handling --------------------------------------------------------------------------


async def test_allowlisted_pull_request_gets_a_neutral_check_run(receiver: Receiver) -> None:
    response = await receiver.deliver("pull_request", pull_request_payload())
    assert response.status_code == 202
    await receiver.dispatcher.drain()
    assert receiver.checks.created == [
        dict(
            installation_id=182623847,
            owner="mpfaffenberger",
            repo="pydantic-ai-claude-code",
            head_sha="0123456789abcdef0123456789abcdef01234567",
            conclusion="neutral",
            title=GREETING_TITLE,
        )
    ]


async def test_redelivered_ids_are_processed_once(receiver: Receiver) -> None:
    first = await receiver.deliver("pull_request", pull_request_payload(), delivery="same")
    second = await receiver.deliver("pull_request", pull_request_payload(), delivery="same")
    await receiver.dispatcher.drain()
    assert (first.status_code, second.json()) == (202, {"status": "duplicate"})
    assert len(receiver.checks.created) == 1


async def test_repos_off_the_allowlist_are_ignored(receiver: Receiver) -> None:
    response = await receiver.deliver("pull_request", pull_request_payload(repo="mpfaffenberger/code_puppy"))
    await receiver.dispatcher.drain()
    assert response.json() == {"status": "ignored", "reason": "repository is not on the allowlist"}
    assert receiver.checks.created == []


@pytest.mark.parametrize("payload", [b"not json", json.dumps({"action": "opened"}).encode()])
async def test_malformed_payloads_are_a_client_error(receiver: Receiver, payload: bytes) -> None:
    response = await receiver.client.post(
        "/webhook",
        content=payload,
        headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d", "X-Hub-Signature-256": signature(payload)},
    )
    assert response.status_code == 400


async def test_missing_event_headers_are_a_client_error(receiver: Receiver) -> None:
    response = await receiver.deliver("ping", {}, headers={"X-GitHub-Delivery": ""})
    assert response.status_code == 400


async def test_a_failing_job_does_not_break_the_receiver(receiver: Receiver) -> None:
    async def boom(**_: object) -> None:
        raise RuntimeError("GitHub is down")

    receiver.checks.create_check_run = boom  # type: ignore[method-assign]
    assert (await receiver.deliver("pull_request", pull_request_payload(), delivery="a")).status_code == 202
    await receiver.dispatcher.drain()
    assert (await receiver.deliver("ping", {}, delivery="b")).json() == {"status": "pong"}


async def test_healthz(receiver: Receiver) -> None:
    assert (await receiver.client.get("/healthz")).json() == {"status": "ok"}


# --- routing (pure) -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "payload", "expected"),
    [
        ("ping", {}, Pong()),
        ("check_run", {"action": "created"}, Ignore("event 'check_run' is not handled yet")),
        ("pull_request", pull_request_payload(action="closed"), Ignore("pull_request action 'closed' needs no review")),
        ("pull_request", pull_request_payload(draft=True), Ignore("draft pull request")),
        (
            "pull_request",
            pull_request_payload(repo="MPFAFFENBERGER/Pydantic-AI-Claude-Code"),
            PullRequestUpdated(
                installation_id=182623847,
                owner="MPFAFFENBERGER",
                repo="Pydantic-AI-Claude-Code",
                number=7,
                head_sha="0123456789abcdef0123456789abcdef01234567",
            ),
        ),
        (
            "pull_request",
            pull_request_payload(action="synchronize"),
            PullRequestUpdated(
                installation_id=182623847,
                owner="mpfaffenberger",
                repo="pydantic-ai-claude-code",
                number=7,
                head_sha="0123456789abcdef0123456789abcdef01234567",
            ),
        ),
    ],
    ids=["ping", "own-check-run-events", "closed", "draft", "case-insensitive-allowlist", "push"],
)
def test_route(event: str, payload: dict[str, Any], expected: object) -> None:
    assert route(event, payload, REPOS) == expected


def test_route_reports_malformed_payloads() -> None:
    with pytest.raises(MalformedPayload, match="repository"):
        route("pull_request", {"action": "opened"}, REPOS)


def test_settings_keep_secrets_out_of_repr() -> None:
    settings = Settings(app_id=1, app_slug="s", webhook_secret="hunter2", private_key="-----BEGIN", repos=REPOS)
    assert "hunter2" not in repr(settings) and "BEGIN" not in repr(settings)
    assert settings.bot_login == "s[bot]"


def test_allowlist_entries_must_be_owner_slash_name() -> None:
    assert parse_repos(" A/b , c/D,, ") == frozenset({"a/b", "c/d"})
    with pytest.raises(ValueError, match="owner/name"):
        parse_repos("just-a-name")
