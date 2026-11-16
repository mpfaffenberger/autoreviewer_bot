"""HTTP edge: verify the signature, drop duplicates, route, answer fast. Everything slow is a job."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from githubkit.webhooks import verify

from autoreviewer.web.config import Settings
from autoreviewer.web.dispatch import Dispatcher
from autoreviewer.web.github import CheckRuns, GitHubApp
from autoreviewer.web.routing import Ignore, MalformedPayload, Pong, PullRequestUpdated, route

logger = logging.getLogger("autoreviewer.web")

GREETING_TITLE = "autoreviewer is listening"


def _reply(status_code: int, **body: str) -> JSONResponse:
    return JSONResponse(body, status_code=status_code)


def create_app(settings: Settings, checks: CheckRuns, dispatcher: Dispatcher | None = None) -> FastAPI:
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
            case PullRequestUpdated():
                dispatcher.submit(
                    f"greet {decision.owner}/{decision.repo}#{decision.number}", lambda: _greet(checks, decision)
                )
                return _reply(202, status="accepted")

    return app


async def _greet(checks: CheckRuns, pull: PullRequestUpdated) -> None:
    """M0's whole job: prove the write path with a neutral check run. Reviews replace this."""
    await checks.create_check_run(
        installation_id=pull.installation_id,
        owner=pull.owner,
        repo=pull.repo,
        head_sha=pull.head_sha,
        conclusion="neutral",
        title=GREETING_TITLE,
        summary=(
            f"Received and verified the webhook for #{pull.number} at `{pull.head_sha[:7]}`. "
            "Reviews are not enabled yet."
        ),
    )


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    logger.info(
        "starting as %s; acting on %s", settings.bot_login, ", ".join(sorted(settings.repos)) or "no repositories"
    )
    app = create_app(settings, GitHubApp.from_key(settings.app_id, settings.private_key))
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")  # only reachable on homelab-public
