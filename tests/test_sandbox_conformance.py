"""Core's own backend contract, run against the whole chain: client -> unix socket -> daemon -> provider."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite

from autoreviewer.sandbox.client import SandboxdBackend, SandboxdClient
from autoreviewer.sandbox.daemon import Limits
from autoreviewer.sandbox.protocol import SandboxSpec
from autoreviewer.sandbox.providers import SandboxProvider
from tests.conftest import LocalDirProvider, running_daemon


class TestSandboxdConformance(WorkspaceBackendSuite):
    @pytest.fixture
    def provider(self) -> SandboxProvider:
        return LocalDirProvider()

    @pytest.fixture
    def spec(self) -> SandboxSpec:
        return SandboxSpec()

    @pytest.fixture
    async def client(self, provider: SandboxProvider) -> AsyncIterator[SandboxdClient]:
        async with running_daemon(provider, Limits(max_sandboxes=16)) as client:
            yield client

    @pytest.fixture
    async def backend(self, client: SandboxdClient, spec: SandboxSpec) -> AsyncIterator[WorkspaceBackend]:
        backend = SandboxdBackend(client, spec=spec)
        yield backend
        await backend.destroy()

    @pytest.fixture
    def fresh_backend(self, client: SandboxdClient, spec: SandboxSpec) -> Callable[[], WorkspaceBackend]:
        return lambda: SandboxdBackend(client, spec=spec)

    @pytest.fixture
    def attach_backend(self, client: SandboxdClient) -> Callable[[WorkspaceRef], WorkspaceBackend]:
        return lambda ref: SandboxdBackend(client, ref=ref)

    @pytest.fixture
    def destroy_environment(self) -> Callable[[WorkspaceBackend], Awaitable[None]]:
        async def destroy(backend: WorkspaceBackend) -> None:
            assert isinstance(backend, SandboxdBackend)
            await backend.destroy()

        return destroy
