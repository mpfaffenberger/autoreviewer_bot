"""The webhook receiver: signature checks, dedupe, routing, and the greeting job."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest

from autoreviewer.web.app import create_app
from autoreviewer.web.config import Settings
from autoreviewer.web.routing import (
    Ignore,
    MalformedPayload,
    Pong,
    PullRequestClosed,
    PullRequestUpdated,
    ReviewRequested,
    ThreadReply,
    is_review_command,
    route,
)

pytestmark = pytest.mark.anyio

SECRET = "test-webhook-secret"
REPO = "mpfaffenberger/pydantic-ai-claude-code"
SLUG = "chaos-emerald-reviewer"


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
class FakeHandler:
    handled: list[PullRequestUpdated] = field(default_factory=list[PullRequestUpdated])
    fail: bool = False

    async def __call__(self, pull: PullRequestUpdated) -> None:
        if self.fail:
            raise RuntimeError("GitHub is down")
        self.handled.append(pull)


@dataclass
class Receiver:
    client: httpx2.AsyncClient
    handler: FakeHandler

    async def deliver(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        delivery: str = "d-1",
        sign: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx2.Response:
        body = json.dumps(payload).encode()
        sent = {
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": sign if sign is not None else signature(body),
        }
        return await self.client.post("/webhook", content=body, headers={**sent, **(headers or {})})


@pytest.fixture
async def receiver() -> AsyncIterator[Receiver]:
    settings = Settings(app_id=1, app_slug="chaos-emerald-reviewer", webhook_secret=SECRET, private_key="unused")
    handler = FakeHandler()
    app = create_app(settings, handler)
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)  # see the 500 a failure produces
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Receiver(client, handler)


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
    assert receiver.handler.handled == []


async def test_a_sha1_signature_header_alone_is_not_enough(receiver: Receiver) -> None:
    body = json.dumps({"zen": "x"}).encode()
    sha1 = "sha1=" + hmac.new(SECRET.encode(), body, hashlib.sha1).hexdigest()
    response = await receiver.client.post(
        "/webhook", content=body, headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "d", "X-Hub-Signature": sha1}
    )
    assert response.status_code == 401


# --- delivery handling --------------------------------------------------------------------------


async def test_a_pull_request_is_handed_to_the_review_job(receiver: Receiver) -> None:
    response = await receiver.deliver("pull_request", pull_request_payload())
    assert response.status_code == 202
    assert receiver.handler.handled == [
        PullRequestUpdated(
            installation_id=182623847,
            owner="mpfaffenberger",
            repo="pydantic-ai-claude-code",
            number=7,
            head_sha="0123456789abcdef0123456789abcdef01234567",
        )
    ]


async def test_redeliveries_are_handed_off_again_because_the_workflow_id_dedupes(receiver: Receiver) -> None:
    for _ in range(2):
        assert (await receiver.deliver("pull_request", pull_request_payload(), delivery="same")).status_code == 202
    assert len(receiver.handler.handled) == 2  # one review regardless: see test_durable.py


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


async def test_a_failed_enqueue_is_a_500_so_github_can_redeliver(receiver: Receiver) -> None:
    receiver.handler.fail = True
    assert (await receiver.deliver("pull_request", pull_request_payload(), delivery="a")).status_code == 500
    receiver.handler.fail = False
    assert (await receiver.deliver("ping", {}, delivery="b")).json() == {"status": "pong"}


async def test_healthz(receiver: Receiver) -> None:
    assert (await receiver.client.get("/healthz")).json() == {"status": "ok"}


# --- routing (pure) -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "payload", "expected"),
    [
        ("ping", {}, Pong()),
        ("check_run", {"action": "created"}, Ignore("event 'check_run' is not handled yet")),
        (
            "pull_request",
            pull_request_payload(action="closed"),
            PullRequestClosed(182623847, "mpfaffenberger", "pydantic-ai-claude-code", 7),
        ),
        (
            "pull_request",
            pull_request_payload(action="labeled"),
            Ignore("pull_request action 'labeled' needs no review"),
        ),
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
    ids=["ping", "own-check-run-events", "closed", "labeled", "draft", "mixed-case-repo", "push"],
)
def test_route(event: str, payload: dict[str, Any], expected: object) -> None:
    assert route(event, payload, SLUG) == expected


def test_route_reports_malformed_payloads() -> None:
    with pytest.raises(MalformedPayload, match="repository"):
        route("pull_request", {"action": "opened"}, SLUG)


def test_settings_keep_secrets_out_of_repr() -> None:
    settings = Settings(app_id=1, app_slug="s", webhook_secret="hunter2", private_key="-----BEGIN")
    assert "hunter2" not in repr(settings) and "BEGIN" not in repr(settings)
    assert settings.bot_login == "s[bot]"


async def test_a_closed_pull_request_reaches_the_closed_handler() -> None:
    settings = Settings(app_id=1, app_slug="s", webhook_secret=SECRET, private_key="unused")
    closed: list[PullRequestClosed] = []

    async def remember_close(event: PullRequestClosed) -> None:
        closed.append(event)

    app = create_app(settings, FakeHandler(), on_pull_request_closed=remember_close)
    body = json.dumps(pull_request_payload(action="closed")).encode()
    headers = {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "c", "X-Hub-Signature-256": signature(body)}
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/webhook", content=body, headers=headers)
    assert response.json() == {"status": "closed"}
    assert closed == [PullRequestClosed(182623847, "mpfaffenberger", "pydantic-ai-claude-code", 7)]


# --- `@chaos-emerald-reviewer review` comments ------------------------------------------------------


def comment_payload(
    body: str = "@chaos-emerald-reviewer review",
    *,
    commenter: str = "someone",
    association: str = "NONE",
    pr_author: str = "someone",
    action: str = "created",
    on_pull_request: bool = True,
    repo: str = REPO,
) -> dict[str, Any]:
    owner, name = repo.split("/")
    issue: dict[str, Any] = {"number": 7, "user": {"login": pr_author}}
    if on_pull_request:
        issue["pull_request"] = {"url": "https://api.github.com/..."}
    return {
        "action": action,
        "installation": {"id": 182623847},
        "repository": {"full_name": repo, "name": name, "owner": {"login": owner}},
        "issue": issue,
        "comment": {"id": 99, "body": body, "user": {"login": commenter}, "author_association": association},
    }


REQUESTED = ReviewRequested(
    installation_id=182623847, owner="mpfaffenberger", repo="pydantic-ai-claude-code", number=7, comment_id=99
)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (comment_payload(), REQUESTED),
        (comment_payload(commenter="dependabot[bot]", pr_author="dependabot[bot]"), REQUESTED),
        (comment_payload(commenter="mike", association="COLLABORATOR", pr_author="someone-else"), REQUESTED),
        (comment_payload(commenter="mike", association="OWNER", pr_author="someone-else"), REQUESTED),
        (
            comment_payload(commenter="drive-by", association="CONTRIBUTOR", pr_author="someone-else"),
            Ignore("drive-by may not use the bot on someone else's pull request"),
        ),
        (comment_payload(commenter="chaos-emerald-reviewer[bot]"), Ignore("the bot's own comment")),
        (comment_payload(action="edited"), Ignore("only new comments are read")),
        (comment_payload(on_pull_request=False), Ignore("comment on an issue, not a pull request")),
        (comment_payload("LGTM"), Ignore("no command for the bot")),
    ],
    ids=[
        "pr-author",
        "bot-pr-author",
        "collaborator",
        "owner",
        "stranger",
        "itself",
        "edit",
        "issue",
        "chat",
    ],
)
def test_route_review_commands(payload: dict[str, Any], expected: object) -> None:
    assert route("issue_comment", payload, SLUG) == expected


@pytest.mark.parametrize(
    ("body", "command"),
    [
        ("@chaos-emerald-reviewer review", True),
        ("Thanks! @Chaos-Emerald-Reviewer REVIEW please", True),
        ("first line\n  @chaos-emerald-reviewer   review\n", True),
        ("> @chaos-emerald-reviewer review\nquoting the instructions", False),
        ("@chaos-emerald-reviewer reviewed this already", False),
        ("@chaos-emerald-reviewer-two review", False),
        ("me@chaos-emerald-reviewer review", False),
        ("@chaos-emerald-reviewer what do you think?", False),
    ],
)
def test_what_counts_as_a_review_command(body: str, command: bool) -> None:
    assert is_review_command(body, SLUG) is command


async def test_a_review_command_reaches_its_handler() -> None:
    requests: list[ReviewRequested] = []

    async def on_request(request: ReviewRequested) -> None:
        requests.append(request)

    settings = Settings(app_id=1, app_slug=SLUG, webhook_secret=SECRET, private_key="unused")
    app = create_app(settings, FakeHandler(), on_review_requested=on_request)
    body = json.dumps(comment_payload()).encode()
    headers = {"X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": "d-9", "X-Hub-Signature-256": signature(body)}
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 202 and requests == [REQUESTED]


@pytest.mark.parametrize("enabled", [True, False])
async def test_a_thread_reply_reaches_its_handler_when_conversations_are_on(enabled: bool) -> None:
    replies: list[ThreadReply] = []

    async def on_reply(reply: ThreadReply) -> None:
        replies.append(reply)

    settings = Settings(app_id=1, app_slug=SLUG, webhook_secret=SECRET, private_key="unused")
    app = create_app(settings, FakeHandler(), on_thread_reply=on_reply if enabled else None)
    delivery = {
        "action": "created",
        "comment": {"id": 9, "in_reply_to_id": 4, "user": {"login": "alice"}, "author_association": "NONE"},
        "pull_request": {"number": 7, "state": "open", "user": {"login": "alice"}},
        "repository": {"name": "demo", "owner": {"login": "mpfaffenberger"}},
        "installation": {"id": 1},
    }
    body = json.dumps(delivery).encode()
    headers = {"X-GitHub-Event": "pull_request_review_comment", "X-GitHub-Delivery": "d-10",
               "X-Hub-Signature-256": signature(body)}  # fmt: skip
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/webhook", content=body, headers=headers)
    assert (response.status_code, len(replies)) == ((202, 1) if enabled else (200, 0))
