"""Receiver settings, read once from the environment (the quadlet's EnvironmentFiles)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

from autoreviewer.servers import (
    CONTEXT_WINDOWS,
    DEEPSEEK,
    DEFAULT_MODEL_LANES,
    FALLBACKS,
    KIMI,
    QWEN,
    REASONING_EFFORTS,
    ReasoningEffort,
)

DEFAULT_PRIVATE_KEY_PATH = "/run/secrets/github-app.pem"

AgentKind = Literal["correctness", "security", "dry", "docs", "tests", "simplify"]
"""Each kind has a finder (the role named after it) and a checker (`<kind>.verifier`)."""
AGENT_KINDS: tuple[AgentKind, ...] = get_args(AgentKind)


def verifier_role(kind: AgentKind) -> str:
    """The role of the agent that checks `kind`'s findings: the one place the `<kind>.verifier` name is made."""
    return f"{kind}.verifier"


AGENT_ROLES = (*AGENT_KINDS, *map(verifier_role, AGENT_KINDS))
_CHECKED_BY_KIMI: tuple[AgentKind, ...] = ("docs", "tests")
DEFAULT_MODELS = {
    **dict.fromkeys(AGENT_KINDS, QWEN),
    **dict.fromkeys(map(verifier_role, AGENT_KINDS), DEEPSEEK),
    **dict.fromkeys(_CHECKED_BY_KIMI, DEEPSEEK),
    **dict.fromkeys(map(verifier_role, _CHECKED_BY_KIMI), KIMI),
}
"""Finders on Qwen (fast), checkers on DeepSeek (better); docs and tests are found by DeepSeek and checked by Kimi,
keeping Qwen's lanes for the rest (Mike, Nov 18). Pin any role to change it (AUTOREVIEWER_MODELS)."""


@dataclass(frozen=True)
class Settings:
    """Which repos the bot reviews is decided in one place only: the App installation's repository
    selection on GitHub. The App is private, so only its owner can install it."""

    app_id: int
    app_slug: str
    webhook_secret: str = field(repr=False)
    private_key: str = field(repr=False)
    logfire_include_content: bool = True
    """Send prompts, model output, and tool results (the reviewed code) to Logfire with each trace."""
    security: bool = True
    """Run the security lens alongside correctness (see review/pipeline.py, review_lens)."""
    dry: bool = True
    """Run the DRY lens: new code that re-implements an existing helper, the stdlib, or a dependency."""
    docs: bool = False
    """Run the docs lens: docstrings, comments, docs, config comments, error messages, and contracts the change
    makes untrue. Off until its evals are in."""
    tests: bool = False
    """Run the tests lens: changed behaviour no test would catch breaking, and tests that can't fail. Off until
    its evals are in."""
    simplify: bool = True
    """Post optional one-click simplifications of the code a PR adds (see review/simplify.py)."""
    block_at_priority: int = 1
    """Confirmed findings at this priority or more urgent (0 = P0 ... 3 = P3) block the merge; 3 blocks on all."""
    soft_deadline_minutes: float = 30
    """After this, every lens wraps up with what it has; a review cut short never approves."""
    hard_deadline_minutes: float = 50
    """After this, the review is cancelled without a verdict (findings already posted stay)."""
    model_lanes: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_MODEL_LANES))
    """At most this many requests at once to each model, across every review (AUTOREVIEWER_MODEL_LANES,
    "model=n,..."; defaults in servers.DEFAULT_MODEL_LANES). Models not listed are unlimited."""
    max_concurrent_jobs: int = 1
    """One review at a time: each already runs a Qwen agent per lens in parallel, about as many as Qwen has lanes
    (servers.DEFAULT_MODEL_LANES). Two at once left most requests waiting and pushed both past their deadline."""
    boodleton_base_url: str = "http://boodleton:8000/v1"
    boodleton_api_key: str = field(default="", repr=False)
    models: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_MODELS))
    """The model each agent role runs on (see models_from); its context window follows (servers.CONTEXT_WINDOWS)."""
    reasoning: Mapping[str, ReasoningEffort] = field(default_factory=dict[str, "ReasoningEffort"])
    """The reasoning effort sent for each role that has one (see reasoning_from); unlisted roles send none."""
    work_dir: Path = Path("/var/lib/autoreviewer/work")
    database: Path = Path("/var/lib/autoreviewer/dbos.sqlite")
    """DBOS system database: queued and in-flight reviews survive restarts here."""
    steps_dir: Path = Path("/var/lib/autoreviewer/steps")
    """Per-PR lens conversations, so a new commit continues the last review (see review/memory.py)."""

    @property
    def bot_login(self) -> str:
        return f"{self.app_slug}[bot]"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        key_path = Path(env.get("AUTOREVIEWER_PRIVATE_KEY_PATH", DEFAULT_PRIVATE_KEY_PATH))
        return cls(
            app_id=int(env["GITHUB_APP_ID"]),
            app_slug=env["GITHUB_APP_SLUG"],
            webhook_secret=env["GITHUB_WEBHOOK_SECRET"],
            private_key=key_path.read_text(),
            max_concurrent_jobs=int(env.get("AUTOREVIEWER_MAX_CONCURRENT_JOBS", cls.max_concurrent_jobs)),
            model_lanes=model_lanes_from(env),
            soft_deadline_minutes=float(env.get("AUTOREVIEWER_SOFT_DEADLINE_MINUTES", cls.soft_deadline_minutes)),
            hard_deadline_minutes=float(env.get("AUTOREVIEWER_HARD_DEADLINE_MINUTES", cls.hard_deadline_minutes)),
            block_at_priority=int(env.get("AUTOREVIEWER_BLOCK_AT_PRIORITY", "1")),
            simplify=parse_bool(env, "AUTOREVIEWER_SIMPLIFY", default=True),
            security=parse_bool(env, "AUTOREVIEWER_SECURITY", default=True),
            dry=parse_bool(env, "AUTOREVIEWER_DRY", default=True),
            docs=parse_bool(env, "AUTOREVIEWER_DOCS", default=False),
            tests=parse_bool(env, "AUTOREVIEWER_TESTS", default=False),
            logfire_include_content=parse_bool(env, "AUTOREVIEWER_LOGFIRE_INCLUDE_CONTENT", default=True),
            boodleton_base_url=env.get("BOODLETON_BASE_URL", cls.boodleton_base_url),
            boodleton_api_key=env["BOODLETON_API_KEY"],
            models=models_from(env),
            reasoning=reasoning_from(env),
            work_dir=Path(env.get("AUTOREVIEWER_WORK_DIR", str(cls.work_dir))),
            database=Path(env.get("AUTOREVIEWER_DATABASE", str(cls.database))),
            steps_dir=Path(env.get("AUTOREVIEWER_STEPS_DIR", str(cls.steps_dir))),
        )


_TRUE, _FALSE = frozenset({"true", "1", "yes", "on"}), frozenset({"false", "0", "no", "off"})


def parse_bool(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    """Strict: a typo must stop startup, not silently pick a side (e.g. keep sending code to Logfire)."""
    value = env.get(name, "").strip().lower()
    if not value:
        return default
    if value in _TRUE | _FALSE:
        return value in _TRUE
    raise ValueError(f"{name}={env[name]!r}: expected true/false (or 1/0, yes/no, on/off)")


def model_lanes_from(env: Mapping[str, str]) -> dict[str, int]:
    """AUTOREVIEWER_MODEL_LANES, or the defaults when it's missing or blank (a placeholder line must never switch
    the limits off). Shared by the bot and the evals."""
    return parse_lanes(env.get("AUTOREVIEWER_MODEL_LANES", "")) or dict(DEFAULT_MODEL_LANES)


def parse_lanes(value: str) -> dict[str, int]:
    """ "model=3,other=4" -> {"model": 3, "other": 4}; a malformed entry stops startup. Blank -> {}."""
    lanes: dict[str, int] = {}
    for name, count in _pairs(value, "AUTOREVIEWER_MODEL_LANES", "model=lanes (lanes >= 1)"):
        if not count.isdigit() or int(count) < 1:
            raise ValueError(f"AUTOREVIEWER_MODEL_LANES: expected model=lanes (lanes >= 1), got {name}={count}")
        lanes[name] = int(count)
    return lanes


def models_from(env: Mapping[str, str]) -> dict[str, str]:
    """Each agent role's model: DEFAULT_MODELS, with AUTOREVIEWER_MODELS ("role=model,...", e.g.
    "security=deepseek-v4.1-flash,security.verifier=qwen3.8-flash-next") pinning roles. Stops startup on an unknown
    role, a model without a known context window, or a finder on the same model as its checker (different models
    make different mistakes; that's the point of checking). Shared by the bot and the evals."""
    models = dict(DEFAULT_MODELS)
    for role, model in _role_pairs(env, "AUTOREVIEWER_MODELS", "role=model"):
        if model not in CONTEXT_WINDOWS:
            raise ValueError(f"AUTOREVIEWER_MODELS: no known context window for {model!r} (servers.CONTEXT_WINDOWS)")
        models[role] = model
    for kind in AGENT_KINDS:
        checker = models[verifier_role(kind)]
        if models[kind] == checker:
            raise ValueError(f"AUTOREVIEWER_MODELS: {kind} and {verifier_role(kind)} are both {models[kind]!r}")
        if FALLBACKS.get(checker) == models[kind]:
            raise ValueError(
                f"AUTOREVIEWER_MODELS: {verifier_role(kind)} ({checker}) falls back to {models[kind]!r}, {kind}'s model"
            )
    return models


def reasoning_from(env: Mapping[str, str]) -> dict[str, ReasoningEffort]:
    """AUTOREVIEWER_REASONING ("role=effort,...", e.g. "docs=high,tests=high"): the reasoning effort each role's
    requests carry. Unlisted roles send none, which leaves the server's default: DeepSeek doesn't think then
    (any effort turns it on), while Qwen and Kimi think by default. Stops startup on an unknown role or effort."""
    # str -> its ReasoningEffort literal: a lookup the type checker can follow, where `in` wouldn't narrow.
    efforts: dict[str, ReasoningEffort] = {effort: effort for effort in REASONING_EFFORTS}
    reasoning: dict[str, ReasoningEffort] = {}
    for role, value in _role_pairs(env, "AUTOREVIEWER_REASONING", "role=effort"):
        if (effort := efforts.get(value)) is None:
            raise ValueError(f"AUTOREVIEWER_REASONING: {role}={value!r}; efforts: {', '.join(REASONING_EFFORTS)}")
        reasoning[role] = effort
    return reasoning


def _role_pairs(env: Mapping[str, str], setting: str, expected: str) -> list[tuple[str, str]]:
    """`setting`'s "role=value" pairs; an unknown role stops startup."""
    pairs = _pairs(env.get(setting, ""), setting, expected)
    for role, _ in pairs:
        if role not in DEFAULT_MODELS:
            raise ValueError(f"{setting}: unknown role {role!r}; roles: {', '.join(AGENT_ROLES)}")
    return pairs


def _pairs(value: str, setting: str, expected: str) -> list[tuple[str, str]]:
    """ "a=1, b=2," -> [("a", "1"), ("b", "2")]; blank entries are skipped, a malformed one stops startup."""
    pairs = []
    for item in filter(None, (part.strip() for part in value.split(","))):
        key, _, val = item.rpartition("=")
        if not key.strip() or not val.strip():
            raise ValueError(f"{setting}: expected {expected}, got {item!r}")
        pairs.append((key.strip(), val.strip()))
    return pairs
