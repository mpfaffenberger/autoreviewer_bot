from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from pydantic_ai.workspaces import LocalWorkspaceBackend

from autoreviewer.review.workspace import Workspace, fetch
from autoreviewer.sandbox.client import SandboxdClient
from autoreviewer.sandbox.daemon import Limits, create_app
from autoreviewer.sandbox.protocol import SandboxSpec
from autoreviewer.sandbox.providers import ProvidedSandbox, SandboxProvider
from tests.review_fakes import GitRepo, make_repo


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"  # smol and the daemon are asyncio-only


class LocalDirProvider:
    """Test double: each sandbox is a temp directory on this host. Exercises the bridge, not isolation."""

    async def create(self, spec: SandboxSpec) -> ProvidedSandbox:
        root = Path(tempfile.mkdtemp(prefix="sbx-")).resolve()

        async def destroy() -> None:
            shutil.rmtree(root, ignore_errors=True)

        return ProvidedSandbox(backend=LocalWorkspaceBackend(root), destroy=destroy)


@contextlib.asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[str]:
    """Run the daemon on a real unix socket for the duration of the block; yield the socket path."""
    # Short path on purpose: unix socket paths max out at 108 bytes and pytest's tmp_path doesn't fit.
    directory = tempfile.mkdtemp(prefix="sbxd-", dir="/tmp")
    socket_path = f"{directory}/s.sock"
    server = uvicorn.Server(uvicorn.Config(app, uds=socket_path, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                task.result()  # surface a startup failure
            await asyncio.sleep(0.01)
        yield socket_path
    finally:
        server.should_exit = True
        await task
        shutil.rmtree(directory, ignore_errors=True)


@contextlib.asynccontextmanager
async def running_daemon(
    provider: SandboxProvider, limits: Limits, **app_kwargs: float
) -> AsyncIterator[SandboxdClient]:
    async with serve(create_app(provider, limits, **app_kwargs)) as socket_path, SandboxdClient(socket_path) as client:
        yield client


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    return make_repo(tmp_path / "origin")


@pytest.fixture
async def workspace(repo: GitRepo, tmp_path: Path) -> Workspace:
    return await fetch(tmp_path / "checkout", url=repo.url, head=repo.head, base=repo.base)
