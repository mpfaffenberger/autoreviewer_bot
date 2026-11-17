"""A git checkout of the PR head, with the merge-base present, for the reviewers to explore.

Verified against GitHub before use: two commits fetched by SHA at depth 1 diff fine; a token passed
through GIT_CONFIG_* environment variables is never written to .git; core.symlinks=false checks a
symlink out as a plain file holding its target, so a PR can't point the read tools outside the repo.

Every git call goes through `run_git`, which ignores system/user config and hooks.
"""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from autoreviewer.github import Repo
from autoreviewer.review.diff import ChangedPath, parse_changes, split_git_diff

BASE, HEAD = "base", "head"
"""Branch names the reviewers use: `git diff base head` is exactly the PR's change."""

GIT_TIMEOUT_S = 300.0
MAX_GIT_OUTPUT = 8 * 1024 * 1024

# Config that must hold for every git call on attacker-controlled content.
_SAFE_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.symlinks=false",
    "core.fsmonitor=false",
    "diff.external=",
    "advice.detachedHead=false",
    "core.quotePath=false",
    "safe.directory=*",
)


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Workspace:
    root: Path
    base: str
    head: str


class RepoSource(Protocol):
    async def checkout(self, repo: Repo, *, head: str, base: str, dest: Path) -> Workspace: ...


def _env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    return {**env, **(extra or {})}


async def run_git(root: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
    command = ["git", *(part for setting in _SAFE_CONFIG for part in ("-c", setting)), *args]
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=root,
        env=_env(env),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=GIT_TIMEOUT_S)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise GitError(f"git {args[0]} timed out after {GIT_TIMEOUT_S:g}s") from None
    if process.returncode != 0:
        raise GitError(f"git {args[0]} failed: {stderr.decode(errors='replace').strip()[:500]}")
    return stdout[:MAX_GIT_OUTPUT].decode(errors="replace")


async def fetch(dest: Path, *, url: str, head: str, base: str, token: str | None = None) -> Workspace:
    """Init `dest`, fetch both commits, check out the head, and name them `base`/`head`."""
    dest.mkdir(parents=True, exist_ok=True)
    await run_git(dest, "init", "-q", ".")
    auth: dict[str, str] = {}
    if token:
        header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        auth = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {header}",
        }
    commits = [head] if base == head else [head, base]
    await run_git(dest, "fetch", "-q", "--depth=1", "--no-tags", url, *commits, env=auth)
    await run_git(dest, "checkout", "-q", "--detach", head)
    await run_git(dest, "branch", "-q", "-f", BASE, base)
    await run_git(dest, "branch", "-q", "-f", HEAD, head)
    return Workspace(root=dest, base=base, head=head)


DIFF_ARGS = ("--no-color", "--no-ext-diff", "--no-textconv", "-M")


async def read_changes(workspace: Workspace) -> tuple[list[ChangedPath], dict[str, str]]:
    """What the PR changes, and each reviewable file's hunks (for anchoring review comments)."""
    root = workspace.root
    name_status = await run_git(root, "diff", "--name-status", "-z", *DIFF_ARGS, BASE, HEAD)
    numstat = await run_git(root, "diff", "--numstat", "-z", *DIFF_ARGS, BASE, HEAD)
    patches = split_git_diff(await run_git(root, "diff", "-U3", *DIFF_ARGS, BASE, HEAD))
    return parse_changes(name_status, numstat), patches


class GitHubSource:
    """Fetches from github.com with a single-repo, read-only installation token."""

    def __init__(self, tokens: _TokenSource) -> None:
        self._tokens = tokens

    async def checkout(self, repo: Repo, *, head: str, base: str, dest: Path) -> Workspace:
        token = await self._tokens.read_token(repo)
        return await fetch(dest, url=repo.clone_url, head=head, base=base, token=token)


class PublicSource:
    """Fetches from github.com anonymously (public repos) or with a caller's token (on-demand reviews)."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token

    async def checkout(self, repo: Repo, *, head: str, base: str, dest: Path) -> Workspace:
        return await fetch(dest, url=repo.clone_url, head=head, base=base, token=self._token)


class _TokenSource(Protocol):
    async def read_token(self, repo: Repo) -> str: ...
