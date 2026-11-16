"""HTTP edge: verify the signature, drop duplicates, route, answer fast. Everything slow is a job."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from githubkit.webhooks import verify

from autoreviewer.web.config import Settings
from autoreviewer.web.dispatch import Dispatcher
from autoreviewer.web.routing import Ignore, MalformedPayload, Pong, PullRequestUpdated, route

logger = logging.getLogger("autoreviewer.web")

PullRequestHandler = Callable[[PullRequestUpdated], Awaitable[None]]


def _reply(status_code: int, **body: str) -> JSONResponse:
    return JSONResponse(body, status_code=status_code)


def create_app(
    settings: Settings, on_pull_request: PullRequestHandler, dispatcher: Dispatcher | None = None
) -> FastAPI:
    dispatcher = dispatcher or Dispatcher(max_concurrent=settings.max_concurrent_jobs)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await dispatcher.drain()

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
        if not dispatcher.first_time(delivery):
            return _reply(200, status="duplicate")
        try:
            decision = route(event, json.loads(body), settings.repos)
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
                dispatcher.submit(f"review {pull.owner}/{pull.repo}#{pull.number}", lambda: on_pull_request(pull))
                return _reply(202, status="accepted")

    return app


def main() -> None:
    import functools

    import uvicorn

    from autoreviewer import logs
    from autoreviewer.github import GitHubApp
    from autoreviewer.review.agents import boodleton_model, correctness_agent, verifier_agent
    from autoreviewer.review.job import ReviewDeps, review_pull_request

    logs.configure()
    settings = Settings.from_env()
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "starting as %s; acting on %s", settings.bot_login, ", ".join(sorted(settings.repos)) or "no repositories"
    )

    def model(name: str):
        return boodleton_model(name, base_url=settings.boodleton_base_url, api_key=settings.boodleton_api_key)

    lens_model, verifier_model = model(settings.lens_model), model(settings.verifier_model)
    deps = ReviewDeps(
        github=GitHubApp.from_key(settings.app_id, settings.private_key),
        agents=lambda root: (correctness_agent(lens_model, root), verifier_agent(verifier_model, root)),
        work_dir=settings.work_dir,
        models_note=f"correctness by `{settings.lens_model}`, verified by `{settings.verifier_model}`",
    )
    app = create_app(settings, functools.partial(review_pull_request, deps))
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")  # only reachable on homelab-public
