"""Receiver settings, read once from the environment (the quadlet's EnvironmentFiles)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PRIVATE_KEY_PATH = "/run/secrets/github-app.pem"


@dataclass(frozen=True)
class Settings:
    app_id: int
    app_slug: str
    webhook_secret: str = field(repr=False)
    private_key: str = field(repr=False)
    repos: frozenset[str]
    """`owner/name` (lowercase) the bot acts on. Everything else is ignored: deny by default."""
    max_concurrent_jobs: int = 2
    """Reviews run on the shared GPU; two at a time leaves room for everyone else."""
    boodleton_base_url: str = "http://boodleton:8000/v1"
    boodleton_api_key: str = field(default="", repr=False)
    lens_model: str = "qwen3.8-flash-next"
    verifier_model: str = "deepseek-v4.1-flash"
    work_dir: Path = Path("/var/lib/autoreviewer/work")
    database: Path = Path("/var/lib/autoreviewer/dbos.sqlite")
    """DBOS system database: queued and in-flight reviews survive restarts here."""

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
            repos=parse_repos(env.get("AUTOREVIEWER_REPOS", "")),
            max_concurrent_jobs=int(env.get("AUTOREVIEWER_MAX_CONCURRENT_JOBS", "2")),
            boodleton_base_url=env.get("BOODLETON_BASE_URL", cls.boodleton_base_url),
            boodleton_api_key=env["BOODLETON_API_KEY"],
            lens_model=env.get("AUTOREVIEWER_LENS_MODEL", cls.lens_model),
            verifier_model=env.get("AUTOREVIEWER_VERIFIER_MODEL", cls.verifier_model),
            work_dir=Path(env.get("AUTOREVIEWER_WORK_DIR", str(cls.work_dir))),
            database=Path(env.get("AUTOREVIEWER_DATABASE", str(cls.database))),
        )


def parse_repos(value: str) -> frozenset[str]:
    repos = frozenset(item.strip().lower() for item in value.split(",") if item.strip())
    if malformed := sorted(repo for repo in repos if repo.count("/") != 1):
        raise ValueError(f"AUTOREVIEWER_REPOS entries must be owner/name: {malformed}")
    return repos
