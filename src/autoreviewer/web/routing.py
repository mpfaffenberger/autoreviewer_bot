"""Decide what a webhook delivery means. Pure: no I/O, so every rule is a one-line test."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PULL_REQUEST_ACTIONS = frozenset({"opened", "reopened", "synchronize", "ready_for_review"})
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
"""Commenters who may command the bot on anyone's PR. A PR's own author (human or bot) may always."""


@dataclass(frozen=True)
class Ignore:
    reason: str


@dataclass(frozen=True)
class Pong:
    """GitHub's `ping`, sent when the webhook is created or a delivery is redelivered from the UI."""


@dataclass(frozen=True)
class PullRequestUpdated:
    installation_id: int
    owner: str
    repo: str
    number: int
    head_sha: str


@dataclass(frozen=True)
class PullRequestClosed:
    """Closed or merged: whatever the bot remembers about the PR can go."""

    installation_id: int
    owner: str
    repo: str
    number: int


@dataclass(frozen=True)
class ReviewRequested:
    """`@<bot> review` in a PR's conversation: review the PR's current head (e.g. a PR opened before the
    bot was installed, which no push has triggered yet). The payload carries no head SHA; it's looked up."""

    installation_id: int
    owner: str
    repo: str
    number: int
    comment_id: int


@dataclass(frozen=True)
class ThreadReply:
    """A reply in an inline comment thread on an open PR. Whether the thread is one of the bot's findings is
    checked when it's answered (the payload doesn't say who started the thread)."""

    installation_id: int
    owner: str
    repo: str
    number: int
    comment_id: int
    root_id: int
    """The thread's first comment: GitHub points every reply at it."""


Decision = Ignore | Pong | PullRequestUpdated | PullRequestClosed | ReviewRequested | ThreadReply


class MalformedPayload(ValueError):
    """The payload lacks a field its event always carries."""


def route(event: str, payload: Mapping[str, Any], app_slug: str) -> Decision:
    try:
        return _route(event, payload, app_slug)
    except (KeyError, TypeError) as error:
        raise MalformedPayload(f"{event} payload is missing {error}") from error


def _route(event: str, payload: Mapping[str, Any], app_slug: str) -> Decision:
    if event == "ping":
        return Pong()
    if event == "issue_comment":
        return _comment(payload, app_slug)
    if event == "pull_request_review_comment":
        return _thread_reply(payload, app_slug)
    if event != "pull_request":
        return Ignore(f"event {event!r} is not handled yet")
    action = payload["action"]
    if action not in PULL_REQUEST_ACTIONS and action != "closed":
        return Ignore(f"pull_request action {action!r} needs no review")
    repository = payload["repository"]
    if action == "closed":
        return PullRequestClosed(
            installation_id=payload["installation"]["id"],
            owner=repository["owner"]["login"],
            repo=repository["name"],
            number=payload["pull_request"]["number"],
        )
    pull_request = payload["pull_request"]
    if pull_request.get("draft"):
        return Ignore("draft pull request")
    return PullRequestUpdated(
        installation_id=payload["installation"]["id"],
        owner=repository["owner"]["login"],
        repo=repository["name"],
        number=pull_request["number"],
        head_sha=pull_request["head"]["sha"],
    )


def _comment(payload: Mapping[str, Any], app_slug: str) -> Decision:
    if payload["action"] != "created":
        return Ignore("only new comments are read")  # an edit must not re-run a command
    issue, comment, repository = payload["issue"], payload["comment"], payload["repository"]
    if "pull_request" not in issue:
        return Ignore("comment on an issue, not a pull request")
    if refusal := _refused(comment, issue["user"]["login"], app_slug):
        return refusal
    if not is_review_command(comment.get("body") or "", app_slug):
        return Ignore("no command for the bot")
    return ReviewRequested(
        installation_id=payload["installation"]["id"],
        owner=repository["owner"]["login"],
        repo=repository["name"],
        number=issue["number"],
        comment_id=comment["id"],
    )


def _refused(comment: Mapping[str, Any], pr_author: str, app_slug: str) -> Ignore | None:
    """Whom the bot listens to on a PR: never itself; otherwise the PR's author (human or bot) or a maintainer."""
    author = comment["user"]["login"]
    if author.lower() == f"{app_slug}[bot]".lower():
        return Ignore("the bot's own comment")
    if author != pr_author and comment.get("author_association") not in TRUSTED_ASSOCIATIONS:
        return Ignore(f"{author} may not use the bot on someone else's pull request")
    return None


def _thread_reply(payload: Mapping[str, Any], app_slug: str) -> Decision:
    if payload["action"] != "created":
        return Ignore("only new comments are answered")  # an edit must not get a second answer
    comment, pull, repository = payload["comment"], payload["pull_request"], payload["repository"]
    root = comment.get("in_reply_to_id")
    if root is None:
        return Ignore("not a reply in a thread")
    if refusal := _refused(comment, pull["user"]["login"], app_slug):
        return refusal
    if pull["state"] != "open":
        return Ignore("the pull request isn't open")
    return ThreadReply(
        installation_id=payload["installation"]["id"],
        owner=repository["owner"]["login"],
        repo=repository["name"],
        number=pull["number"],
        comment_id=comment["id"],
        root_id=root,
    )


def is_review_command(body: str, app_slug: str) -> bool:
    """`@<slug> review` anywhere in the comment, case-insensitive, outside quoted (`>`) lines."""
    command = re.compile(rf"(?<![\w-])@{re.escape(app_slug)}\s+review\b", re.IGNORECASE)
    return any(command.search(line) for line in body.splitlines() if not line.lstrip().startswith(">"))
