"""Receiver settings, read once from the environment (the quadlet's EnvironmentFiles)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PRIVATE_KEY_PATH = "/run/secrets/github-app.pem"
DEFAULT_MODEL_LANES = {"qwen3.8-flash-next": 3, "deepseek-v4.1-flash": 4}


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
    fused_security: bool = False
    """Review security inside the correctness lens (one agent, a security addendum, its findings checked by
    the security verifier) instead of a separate security lens. Off until the eval says it's as good."""
    dry: bool = True
    """Run the DRY lens: new code that re-implements an existing helper, the stdlib, or a dependency."""
    simplify: bool = True
    """Post optional one-click simplifications of the code a PR adds (see review/simplify.py)."""
    block_at_priority: int = 1
    """Confirmed findings at this priority or more urgent (0 = P0 ... 3 = P3) block the merge; 3 blocks on all."""
    soft_deadline_minutes: float = 30
    """After this, every lens wraps up with what it has; a review cut short never approves."""
    hard_deadline_minutes: float = 50
    """After this, the review is cancelled without a verdict (findings already posted stay)."""
    model_lanes: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_MODEL_LANES))
    """At most this many requests at once to each model, across every review (AUTOREVIEWER_MODEL_LANES, e.g.
    "qwen3.8-flash-next=3,deepseek-v4.1-flash=4"). Qwen's server has 3 lanes; DeepSeek's has more, but it's
    shared. Models not listed are unlimited."""
    max_concurrent_jobs: int = 1
    """One review at a time: each already runs three Qwen agents in parallel (correctness, security, simplify),
    and Qwen's server has 3 lanes. Two at once left 6 of 9 requests waiting and pushed both past their deadline."""
    boodleton_base_url: str = "http://boodleton:8000/v1"
    boodleton_api_key: str = field(default="", repr=False)
    lens_model: str = "qwen3.8-flash-next"
    verifier_model: str = "deepseek-v4.1-flash"
    lens_context_window: int = 262_144
    verifier_context_window: int = 1_048_576
    """Each model's context window in tokens; compaction keeps conversations under 60% of it."""
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
            fused_security=parse_bool(env, "AUTOREVIEWER_FUSED_SECURITY", default=False),
            logfire_include_content=parse_bool(env, "AUTOREVIEWER_LOGFIRE_INCLUDE_CONTENT", default=True),
            boodleton_base_url=env.get("BOODLETON_BASE_URL", cls.boodleton_base_url),
            boodleton_api_key=env["BOODLETON_API_KEY"],
            lens_model=env.get("AUTOREVIEWER_LENS_MODEL", cls.lens_model),
            verifier_model=env.get("AUTOREVIEWER_VERIFIER_MODEL", cls.verifier_model),
            lens_context_window=int(env.get("AUTOREVIEWER_LENS_CONTEXT_WINDOW", cls.lens_context_window)),
            verifier_context_window=int(env.get("AUTOREVIEWER_VERIFIER_CONTEXT_WINDOW", cls.verifier_context_window)),
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
    for item in filter(None, (part.strip() for part in value.split(","))):
        name, _, count = item.rpartition("=")
        if not name or not count.isdigit() or int(count) < 1:
            raise ValueError(f"AUTOREVIEWER_MODEL_LANES: expected model=lanes (lanes >= 1), got {item!r}")
        lanes[name] = int(count)
    return lanes
