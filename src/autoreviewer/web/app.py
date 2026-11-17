"""HTTP edge: verify the signature, drop duplicates, route, answer fast. Everything slow is a job."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from githubkit.webhooks import verify

from autoreviewer.web.config import Settings

if TYPE_CHECKING:
    from pydantic_ai.models.openai import OpenAIChatModel

    from autoreviewer.review.job import AgentFactory
from autoreviewer.web.routing import (
    Ignore,
    MalformedPayload,
    Pong,
    PullRequestClosed,
    PullRequestUpdated,
    ReviewRequested,
    route,
)

logger = logging.getLogger("autoreviewer.web")

PullRequestHandler = Callable[[PullRequestUpdated], Awaitable[object]]
ClosedHandler = Callable[[PullRequestClosed], Awaitable[None]]
RequestHandler = Callable[[ReviewRequested], Awaitable[object]]


def _reply(status_code: int, **body: str) -> JSONResponse:
    return JSONResponse(body, status_code=status_code)


def create_app(
    settings: Settings,
    on_pull_request: PullRequestHandler,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    on_pull_request_closed: ClosedHandler | None = None,
    on_review_requested: RequestHandler | None = None,
) -> FastAPI:
    """`on_pull_request` must be quick (it enqueues); `lifespan` starts and stops whatever runs the work."""
    app = FastAPI(title="autoreviewer", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhook")
    async def webhook(request: Request) -> JSONResponse:
        body = await request.body()
        signature = request.headers.get("x-hub-signature-256", "")
        # sha256 only: githubkit's verify would accept a legacy sha1 signature, which we never want.
        if not signature.startswith("sha256=") or not verify(settings.webhook_secret, body, signature):
            logger.warning("rejected a delivery with a missing or wrong signature")
            return _reply(401, error="invalid signature")
        event = request.headers.get("x-github-event")
        delivery = request.headers.get("x-github-delivery")
        if not event or not delivery:
            return _reply(400, error="missing X-GitHub-Event or X-GitHub-Delivery")
        try:
            decision = route(event, json.loads(body), settings.app_slug)
        except (ValueError, MalformedPayload) as error:
            logger.warning("delivery %s: malformed %s payload: %s", delivery, event, error)
            return _reply(400, error="malformed payload")

        logger.info("delivery %s: %s -> %s", delivery, event, decision)
        match decision:
            case Pong():
                return _reply(200, status="pong")
            case Ignore(reason=reason):
                return _reply(200, status="ignored", reason=reason)
            case PullRequestUpdated() as pull:
                # Awaited, not fire-and-forget: if enqueueing fails, GitHub sees a 500 and can redeliver.
                # Duplicate deliveries are harmless; the review workflow's ID makes them no-ops.
                await on_pull_request(pull)
                return _reply(202, status="accepted")
            case PullRequestClosed() as closed:
                if on_pull_request_closed is not None:
                    await on_pull_request_closed(closed)
                return _reply(200, status="closed")
            case ReviewRequested() as command:
                if on_review_requested is None:
                    return _reply(200, status="ignored", reason="commands are not enabled")
                await on_review_requested(command)  # a failure is a 500, so GitHub's redelivery can retry it
                return _reply(202, status="accepted")

    return app


def build_agent_factory(settings: Settings) -> AgentFactory:
    """(lens, verifier) over a checkout, on the configured Boodleton models: shared by the bot and the CLI."""
    from autoreviewer.review.agents import boodleton_model, correctness_agent, verifier_agent

    def model(name: str) -> OpenAIChatModel:
        return boodleton_model(name, base_url=settings.boodleton_base_url, api_key=settings.boodleton_api_key)

    lens_model, verifier_model = model(settings.lens_model), model(settings.verifier_model)
    return lambda workspace: (
        correctness_agent(lens_model, workspace, context_window=settings.lens_context_window),
        verifier_agent(verifier_model, workspace, context_window=settings.verifier_context_window),
    )


def models_note(settings: Settings) -> str:
    return f"correctness by `{settings.lens_model}`, verified by `{settings.verifier_model}`"


def main() -> None:
    import uvicorn

    from autoreviewer import logs
    from autoreviewer.github import GitHubApp, Repo
    from autoreviewer.review import durable
    from autoreviewer.review.agents import lens_version
    from autoreviewer.review.job import ReviewDeps
    from autoreviewer.review.memory import ReviewMemory, conversation_id
    from autoreviewer.review.workspace import GitHubSource
    from autoreviewer.web.commands import request_review

    logs.configure()
    settings = Settings.from_env()
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    logger.info("starting as %s; reviewing the repositories selected on the App installation", settings.bot_login)

    github = GitHubApp.from_key(settings.app_id, settings.private_key)
    memory = ReviewMemory(settings.steps_dir)
    deps = ReviewDeps(
        github=github,
        source=GitHubSource(github),
        agents=build_agent_factory(settings),
        work_dir=settings.work_dir,
        bot_login=settings.bot_login,
        models_note=models_note(settings),
        memory=memory,
        lens_version=lens_version(settings.lens_model),
        block_at=settings.block_at_priority,
    )
    durable.configure(settings.database)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await durable.launch(deps, concurrency=settings.max_concurrent_jobs)  # also recovers interrupted reviews
        try:
            yield
        finally:
            durable.shutdown()

    async def review_requested(request: ReviewRequested) -> None:
        await request_review(github, request, durable.enqueue_review)

    async def forget(closed: PullRequestClosed) -> None:
        repo = Repo(closed.installation_id, closed.owner, closed.repo)
        memory.forget(conversation_id(repo, closed.number))

    app = create_app(settings, durable.enqueue_review, lifespan, forget, review_requested)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")  # only reachable on homelab-public
