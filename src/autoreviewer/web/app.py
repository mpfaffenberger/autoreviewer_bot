"""HTTP edge: verify the signature, drop duplicates, route, answer fast. Everything slow is a job."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from githubkit.webhooks import verify

from autoreviewer.servers import CONTEXT_WINDOWS, ReasoningEffort
from autoreviewer.web.config import CONVERSATION, AgentKind, Settings, verifier_role

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from autoreviewer.github import GitHubApp
    from autoreviewer.review.conversation import ConversationDeps
    from autoreviewer.review.job import AgentFactory, Lens
    from autoreviewer.review.schemas import LensKind
    from autoreviewer.review.simplify import SimplifierFactory
from autoreviewer.web.routing import (
    Ignore,
    MalformedPayload,
    Pong,
    PullRequestClosed,
    PullRequestUpdated,
    ReviewRequested,
    ThreadReply,
    route,
)

logger = logging.getLogger("autoreviewer.web")

PullRequestHandler = Callable[[PullRequestUpdated], Awaitable[object]]
ClosedHandler = Callable[[PullRequestClosed], Awaitable[None]]
RequestHandler = Callable[[ReviewRequested], Awaitable[object]]
ReplyHandler = Callable[[ThreadReply], Awaitable[object]]


def _reply(status_code: int, **body: str) -> JSONResponse:
    return JSONResponse(body, status_code=status_code)


def create_app(
    settings: Settings,
    on_pull_request: PullRequestHandler,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    on_pull_request_closed: ClosedHandler | None = None,
    on_review_requested: RequestHandler | None = None,
    on_thread_reply: ReplyHandler | None = None,
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
            case ThreadReply() as reply:
                if on_thread_reply is None:
                    return _reply(200, status="ignored", reason="conversations are not enabled")
                await on_thread_reply(reply)  # queues the answer; its workflow ID makes redeliveries no-ops
                return _reply(202, status="accepted")

    return app


@dataclass(frozen=True)
class ModelPlan:
    """Which model each agent runs on (config.models_from), built on Boodleton, each model limited to its lanes. The
    bot and the evals both build their agents from one, so they can't drift apart."""

    models: Mapping[str, str]
    base_url: str
    api_key: str = field(repr=False)
    lanes: Mapping[str, int]
    reasoning: Mapping[str, ReasoningEffort] = field(default_factory=dict[str, "ReasoningEffort"])
    """Role -> reasoning effort (config.reasoning_from)."""

    @classmethod
    def from_settings(cls, settings: Settings) -> ModelPlan:
        return cls(
            settings.models,
            settings.boodleton_base_url,
            settings.boodleton_api_key,
            settings.model_lanes,
            settings.reasoning,
        )

    def finder(self, kind: AgentKind) -> tuple[Model, int]:
        """The model and its context window for `kind`'s finder (a lens, or the simplifier's proposer)."""
        return self._build(kind)

    def verifier(self, kind: AgentKind) -> tuple[Model, int]:
        """The model and its context window for the agent that checks `kind`'s findings."""
        return self._build(verifier_role(kind))

    def conversation(self) -> tuple[Model, int]:
        """The model and its context window for answering replies under the bot's findings."""
        return self._build(CONVERSATION)

    def _build(self, role: str) -> tuple[Model, int]:
        from autoreviewer.review.agents import boodleton_model

        name = self.models[role]
        model = boodleton_model(
            name,
            base_url=self.base_url,
            api_key=self.api_key,
            lanes=self.lanes.get(name),
            reasoning=self.reasoning.get(role),
        )
        return model, CONTEXT_WINDOWS[name]


def build_agent_factory(settings: Settings) -> AgentFactory:
    """(lens, verifier) for correctness over a checkout, on the configured models: shared by the bot and the CLI."""
    return correctness_agents(ModelPlan.from_settings(settings))


def correctness_agents(plan: ModelPlan) -> AgentFactory:
    from autoreviewer.review.agents import correctness_agent, verifier_agent

    lens_model, lens_window = plan.finder("correctness")
    verifier_model, verifier_window = plan.verifier("correctness")
    return lambda workspace: (
        correctness_agent(lens_model, workspace, context_window=lens_window),
        verifier_agent(verifier_model, workspace, context_window=verifier_window),
    )


MAX_ADVISORY_POSTED = 5
"""Each advisory lens (DRY, docs, tests) posts at most its 5 best findings. The eval's clean 786-line commit drew 8
DRY comments, mostly true but too many to read; 3 dropped real findings for little gain; 5 keeps most of them
while a PR's blocking findings stay easy to see (Mike, Nov 18)."""

LENS_NAMES: dict[LensKind, str] = {"security": "security", "dry": "DRY", "docs": "docs", "tests": "tests"}
"""How the review footer names each lens; lenses run and are listed in this order."""


def enabled_lenses(settings: Settings) -> list[LensKind]:
    """The lenses switched on beside correctness, in LENS_NAMES order: Settings has one switch per lens, named after
    it."""
    return [kind for kind in LENS_NAMES if getattr(settings, kind)]


def build_lenses(settings: Settings) -> tuple[Lens, ...]:
    """The enabled lenses beside correctness, each on its own models (Settings.models)."""
    plan = ModelPlan.from_settings(settings)
    return tuple(lens(kind, lens_agents(plan, kind)) for kind in enabled_lenses(settings))


def lens_agents(plan: ModelPlan, kind: LensKind) -> AgentFactory:
    """(lens, verifier) for `kind` over a checkout, on the plan's models for it: shared by the bot and the evals."""
    from autoreviewer.review.agents import lens_pair

    lens_model, lens_window = plan.finder(kind)
    verifier_model, verifier_window = plan.verifier(kind)
    return lambda workspace: lens_pair(
        kind, lens_model, verifier_model, workspace, lens_window=lens_window, verifier_window=verifier_window
    )


def lens(kind: LensKind, agents: AgentFactory) -> Lens:
    """A lens with its production settings, for the bot and the evals alike. Security reviews the whole PR every
    time (it can block); the others are advice: the changed files only when continuing, and at most
    MAX_ADVISORY_POSTED."""
    from autoreviewer.review.agents import security_references
    from autoreviewer.review.job import Lens

    if kind == "security":
        return Lens("security", agents, instructions=security_references)
    return Lens(kind, agents, whole_pr=False, max_posted=MAX_ADVISORY_POSTED)


def build_simplifier_factory(settings: Settings) -> SimplifierFactory | None:
    """None when simplifications are off."""
    return simplifier_agents(ModelPlan.from_settings(settings)) if settings.simplify else None


def simplifier_agents(plan: ModelPlan) -> SimplifierFactory:
    """Proposals and checks on the plan's models for the simplifier."""
    from autoreviewer.review.agents import simplify_agent, simplify_checker_agent
    from autoreviewer.review.simplify import Simplifier

    propose_model, propose_window = plan.finder("simplify")
    check_model, check_window = plan.verifier("simplify")
    return lambda workspace: Simplifier(
        simplify_agent(propose_model, workspace, context_window=propose_window),
        simplify_checker_agent(check_model, workspace, context_window=check_window),
    )


def models_note(settings: Settings) -> str:
    """The review footer's "correctness and security by `a`, verified by `b`", one clause per pair of models."""
    enabled: list[tuple[AgentKind, str]] = [
        ("correctness", "correctness"),
        *((kind, LENS_NAMES[kind]) for kind in enabled_lenses(settings)),
    ]
    groups: dict[tuple[str, str], list[str]] = {}
    for kind, name in enabled:
        groups.setdefault((settings.models[kind], settings.models[verifier_role(kind)]), []).append(name)
    return "; ".join(
        f"{_and(names)} by `{finder}`, verified by `{checker}`" for (finder, checker), names in groups.items()
    )


def _and(names: list[str]) -> str:
    return ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else names[0]


def thread_reply_handler(settings: Settings) -> ReplyHandler | None:
    """Replies under the bot's findings are queued for an answer only with AUTOREVIEWER_CONVERSATIONS on."""
    from autoreviewer.review import durable

    return durable.enqueue_answer if settings.conversations else None


def conversation_deps(settings: Settings, github: GitHubApp) -> ConversationDeps:
    """Answers on the conversation role's model, over a fresh checkout of the PR's head."""
    from autoreviewer.review.agents import conversation_agent
    from autoreviewer.review.conversation import ConversationDeps
    from autoreviewer.review.workspace import GitHubSource

    model, window = ModelPlan.from_settings(settings).conversation()
    return ConversationDeps(
        github=github,
        source=GitHubSource(github),
        agents=lambda workspace: conversation_agent(model, workspace, context_window=window),
        work_dir=settings.work_dir,
        bot_login=settings.bot_login,
    )


def main() -> None:
    import functools

    import logfire
    import uvicorn

    from autoreviewer import logs, telemetry
    from autoreviewer.github import GitHubApp, Repo
    from autoreviewer.review import durable
    from autoreviewer.review.agents import lens_version
    from autoreviewer.review.job import ReviewDeps
    from autoreviewer.review.memory import ReviewMemory, conversation_id
    from autoreviewer.review.workspace import GitHubSource
    from autoreviewer.web.commands import request_review

    settings = Settings.from_env()
    telemetry.configure(service="autoreviewer", include_content=settings.logfire_include_content)
    logs.configure(to_logfire=True)
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
        lens_version=lens_version(settings.models["correctness"], settings.reasoning.get("correctness")),
        block_at=settings.block_at_priority,
        simplifier=build_simplifier_factory(settings),
        lenses=build_lenses(settings),
        soft_deadline_s=settings.soft_deadline_minutes * 60,
        hard_deadline_s=settings.hard_deadline_minutes * 60,
    )
    durable.configure(settings.database)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Also recovers interrupted reviews and answers.
        await durable.launch(deps, conversation_deps(settings, github), concurrency=settings.max_concurrent_jobs)
        try:
            yield
        finally:
            durable.shutdown()

    async def review_requested(request: ReviewRequested) -> None:
        # An explicit request retries a review of this commit that was cancelled or crashed.
        await request_review(github, request, functools.partial(durable.enqueue_review, retry_failed=True))

    async def forget(closed: PullRequestClosed) -> None:
        repo = Repo(closed.installation_id, closed.owner, closed.repo)
        memory.forget(conversation_id(repo, closed.number))

    app = create_app(
        settings,
        durable.enqueue_review,
        lifespan,
        forget,
        review_requested,
        thread_reply_handler(settings),
    )
    logfire.instrument_fastapi(app)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")  # only reachable on homelab-public
