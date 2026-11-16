"""The reviewer's side: a `WorkspaceBackend` whose commands run in a `sandboxd` microVM.

Pass it as `agent.run(..., workspace=SandboxdBackend(...))` and harness tools (`Shell`, `FileSystem`,
`Coder`, ...) work unchanged; file operations derive from the shell, as for any command-only backend.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Self

import httpx
from pydantic_ai.workspaces import CommandResult, WorkspaceCommand, WorkspaceError, WorkspaceRef

from autoreviewer.sandbox.protocol import (
    API,
    PROVIDER,
    Created,
    ErrorBody,
    RunRequest,
    RunResult,
    SandboxSpec,
    WorkingDir,
    decode_error,
)

_CONNECT_TIMEOUT_S = 10.0
_TRANSPORT_GRACE_S = 30.0
"""Extra time past a command's own timeout before the HTTP layer gives up on the daemon's answer."""


class SandboxdClient:
    """Thin typed wrapper over the daemon's HTTP API on a unix socket."""

    def __init__(self, socket_path: str, *, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path), base_url="http://sandboxd"
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def create(self, spec: SandboxSpec) -> str:
        # Booting can include pulling an image the first time, hence no read timeout.
        response = await self._request("POST", API, json=spec.model_dump(), read_timeout=None)
        return Created.model_validate_json(response.content).id

    async def working_dir(self, sandbox_id: str) -> str:
        response = await self._request("GET", f"{API}/{sandbox_id}/working_dir", read_timeout=120.0)
        return WorkingDir.model_validate_json(response.content).working_dir

    async def run(self, sandbox_id: str, request: RunRequest) -> CommandResult:
        read_timeout = None if request.timeout is None else request.timeout + _TRANSPORT_GRACE_S
        response = await self._request(
            "POST", f"{API}/{sandbox_id}/run", json=request.model_dump(), read_timeout=read_timeout
        )
        result = RunResult.model_validate_json(response.content)
        return CommandResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)

    async def destroy(self, sandbox_id: str) -> None:
        await self._request("DELETE", f"{API}/{sandbox_id}", read_timeout=120.0)

    async def _request(
        self, method: str, url: str, *, json: object = None, read_timeout: float | None
    ) -> httpx.Response:
        timeout = httpx.Timeout(_CONNECT_TIMEOUT_S, read=read_timeout)
        try:
            response = await self._http.request(method, url, json=json, timeout=timeout)
        except httpx.TransportError as error:
            raise WorkspaceError(f"sandboxd is unreachable: {error!r}") from error
        if response.is_success:
            return response
        try:
            body = ErrorBody.model_validate_json(response.content)
        except ValueError:
            raise WorkspaceError(f"sandboxd answered HTTP {response.status_code}: {response.text[:500]}") from None
        raise decode_error(body)


class SandboxdBackend:
    """A `WorkspaceBackend` + `SupportsCommands` for one `sandboxd` sandbox.

    Built without I/O. The first operation creates the sandbox from `spec`, or attaches to the one
    `ref` names. It never destroys the sandbox; whoever holds the ref does, with `destroy()`.
    """

    def __init__(self, client: SandboxdClient, *, spec: SandboxSpec | None = None, ref: WorkspaceRef | None = None):
        if ref is not None and ref.provider != PROVIDER:
            raise ValueError(f"ref is for {ref.provider!r}, not {PROVIDER!r}")
        if (spec is None) == (ref is None):
            raise ValueError("pass exactly one of `spec` (create a sandbox) or `ref` (attach to one)")
        self._client = client
        self._spec = spec
        self._ref = ref
        self._lock = asyncio.Lock()
        self._creating: asyncio.Future[str] | None = None
        self._working_dir: str | None = None

    @property
    def ref(self) -> WorkspaceRef | None:
        return self._ref

    async def working_dir(self) -> str:
        if self._working_dir is None:
            self._working_dir = await self._client.working_dir(await self._sandbox_id())
        return self._working_dir

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        request = RunRequest(
            command=command if isinstance(command, str) else list(command),
            shell=shell,
            env=dict(env) if env is not None else None,
            timeout=timeout,
        )
        return await self._client.run(await self._sandbox_id(), request)

    async def destroy(self) -> None:
        """Delete the sandbox, if one was created. Idempotent."""
        if self._ref is not None:
            await self._client.destroy(self._ref.id)

    async def _sandbox_id(self) -> str:
        async with self._lock:  # concurrent first operations share one sandbox
            if self._ref is None:
                assert self._spec is not None
                if self._creating is None:
                    self._creating = asyncio.ensure_future(self._client.create(self._spec))
                    # Recorded by callback, so a caller cancelled mid-create still leaves a ref to clean up.
                    self._creating.add_done_callback(self._record)
                await asyncio.shield(self._creating)
            assert self._ref is not None
            return self._ref.id

    def _record(self, future: asyncio.Future[str]) -> None:
        if future.cancelled() or future.exception() is not None:
            self._creating = None  # let the next operation try again
            return
        self._ref = WorkspaceRef(provider=PROVIDER, id=future.result())
