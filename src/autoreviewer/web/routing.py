"""Decide what a webhook delivery means. Pure: no I/O, so every rule is a one-line test."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PULL_REQUEST_ACTIONS = frozenset({"opened", "reopened", "synchronize", "ready_for_review"})


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


Decision = Ignore | Pong | PullRequestUpdated


class MalformedPayload(ValueError):
    """The payload lacks a field its event always carries."""


def route(event: str, payload: Mapping[str, Any], repos: frozenset[str]) -> Decision:
    try:
        return _route(event, payload, repos)
    except (KeyError, TypeError) as error:
        raise MalformedPayload(f"{event} payload is missing {error}") from error


def _route(event: str, payload: Mapping[str, Any], repos: frozenset[str]) -> Decision:
    if event == "ping":
        return Pong()
    if event != "pull_request":
        return Ignore(f"event {event!r} is not handled yet")
    if (action := payload["action"]) not in PULL_REQUEST_ACTIONS:
        return Ignore(f"pull_request action {action!r} needs no review")
    repository = payload["repository"]
    if repository["full_name"] not in repos:
        return Ignore("repository is not on the allowlist")
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
