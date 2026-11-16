"""`sandboxd`: owns the microVMs, so the process that holds API keys and GitHub tokens never does.

It runs as its own OS user (`smolrunner`) because smolvm's VMM runs in-process with that user's
permissions: a guest that escapes the VM lands in an account with no secrets and a uid-scoped
firewall. Clients reach it over a unix socket whose group is the only authentication.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import grp
import logging
import os
import socket
import stat
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic_ai.workspaces import WorkspaceError, WorkspaceUnavailableError

from autoreviewer.sandbox.protocol import (
    API,
    CapacityError,
    Created,
    ForbiddenSpecError,
    RunRequest,
    RunResult,
    SandboxSpec,
    WorkingDir,
    encode_error,
)
from autoreviewer.sandbox.providers import ProvidedSandbox, SandboxProvider

logger = logging.getLogger("autoreviewer.sandboxd")

CLIENT_CLOSED_REQUEST = 499


@dataclass(frozen=True)
class Limits:
    """Ceilings the daemon enforces whatever a client asks for."""

    max_sandboxes: int = 2
    max_age_s: float = 20 * 60
    max_cpus: int = 4
    max_memory_mb: int = 3072
    permitted_hosts: frozenset[str] = frozenset()
    """Hosts a sandbox may be allowed to reach. Empty: no sandbox gets network."""
    permitted_images: frozenset[str | None] | None = None
    """Images a sandbox may boot (`None` in the set is smolvm's own Alpine). `None`: any image."""

    def check(self, spec: SandboxSpec) -> None:
        if spec.cpus > self.max_cpus:
            raise ForbiddenSpecError(f"cpus={spec.cpus} is above the limit of {self.max_cpus}")
        if spec.memory_mb > self.max_memory_mb:
            raise ForbiddenSpecError(f"memory_mb={spec.memory_mb} is above the limit of {self.max_memory_mb}")
        if denied := sorted(set(spec.allow_hosts) - self.permitted_hosts):
            raise ForbiddenSpecError(f"hosts not permitted: {', '.join(denied)}")
        if self.permitted_images is not None and spec.image not in self.permitted_images:
            raise ForbiddenSpecError(f"image not permitted: {spec.image!r}")


@dataclass
class _Entry:
    provided: ProvidedSandbox
    created_at: float
    inflight: set[asyncio.Task[object]] = field(default_factory=set[asyncio.Task[object]])


class SandboxRegistry:
    """Lifecycle and policy, independent of HTTP: create within limits, look up, destroy, reap."""

    def __init__(self, provider: SandboxProvider, limits: Limits) -> None:
        self._provider = provider
        self._limits = limits
        self._entries: dict[str, _Entry] = {}
        self._creating = 0

    async def create(self, spec: SandboxSpec) -> str:
        self._limits.check(spec)
        if len(self._entries) + self._creating >= self._limits.max_sandboxes:
            raise CapacityError(f"sandboxd is at its limit of {self._limits.max_sandboxes} sandboxes; retry later")
        self._creating += 1
        try:
            # Shielded: a client that disconnects mid-boot must not leave a VM nobody can find to delete.
            provided = await asyncio.shield(self._provider.create(spec))
        except WorkspaceError:
            raise
        except Exception as error:
            # Engine failures (e.g. smol.SmolError) aren't part of the wire contract; keep their message.
            logger.exception("creating a sandbox failed")
            raise WorkspaceError(f"sandboxd could not create the sandbox: {error}") from error
        finally:
            self._creating -= 1
        sandbox_id = uuid.uuid4().hex
        self._entries[sandbox_id] = _Entry(provided, time.monotonic())
        logger.info("created %s image=%s hosts=%s", sandbox_id, spec.image, ",".join(spec.allow_hosts) or "-")
        return sandbox_id

    def get(self, sandbox_id: str) -> _Entry:
        try:
            return self._entries[sandbox_id]
        except KeyError:
            raise WorkspaceUnavailableError(
                f"sandbox {sandbox_id!r} does not exist (deleted, expired, or daemon restarted)"
            ) from None

    async def destroy(self, sandbox_id: str) -> None:
        entry = self._entries.pop(sandbox_id, None)
        if entry is None:
            return
        for task in entry.inflight:
            task.cancel()
        await entry.provided.destroy()
        logger.info("destroyed %s", sandbox_id)

    async def reap(self) -> None:
        cutoff = time.monotonic() - self._limits.max_age_s
        for sandbox_id in [sid for sid, entry in self._entries.items() if entry.created_at < cutoff]:
            logger.warning("reaping %s: older than %.0fs", sandbox_id, self._limits.max_age_s)
            await self.destroy(sandbox_id)

    async def destroy_all(self) -> None:
        for sandbox_id in list(self._entries):
            with contextlib.suppress(Exception):
                await self.destroy(sandbox_id)


async def _until_disconnected(request: Request) -> None:
    # The body is already read, so the next ASGI message only arrives when the client goes away.
    while (await request.receive())["type"] != "http.disconnect":
        pass


def _error_response(error: Exception) -> JSONResponse:
    body = encode_error(error)
    if body is None:
        raise error
    return JSONResponse(body.model_dump(), status_code=body.status)


def create_app(provider: SandboxProvider, limits: Limits, *, reap_interval_s: float = 30.0) -> FastAPI:
    registry = SandboxRegistry(provider, limits)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async def reaper() -> None:
            while True:
                await asyncio.sleep(reap_interval_s)
                try:
                    await registry.reap()
                except Exception:
                    logger.exception("reaper failed")

        task = asyncio.create_task(reaper())
        try:
            yield
        finally:
            task.cancel()
            await registry.destroy_all()

    app = FastAPI(title="autoreviewer sandboxd", lifespan=lifespan, docs_url=None, redoc_url=None)

    # Explicit response models: endpoints also return error `Response`s, which FastAPI can't infer from a union.
    @app.post(API, status_code=201, response_model=Created)
    async def create(spec: SandboxSpec) -> Created | JSONResponse:
        try:
            return Created(id=await registry.create(spec))
        except Exception as error:
            return _error_response(error)

    @app.get(API + "/{sandbox_id}/working_dir", response_model=WorkingDir)
    async def working_dir(sandbox_id: str) -> WorkingDir | JSONResponse:
        try:
            return WorkingDir(working_dir=await registry.get(sandbox_id).provided.backend.working_dir())
        except Exception as error:
            return _error_response(error)

    @app.post(API + "/{sandbox_id}/run", response_model=RunResult)
    async def run(sandbox_id: str, body: RunRequest, request: Request) -> RunResult | Response:
        try:
            entry = registry.get(sandbox_id)
        except Exception as error:
            return _error_response(error)
        command = entry.provided.backend.run(body.command, shell=body.shell, env=body.env, timeout=body.timeout)
        task: asyncio.Task[object] = asyncio.ensure_future(command)
        entry.inflight.add(task)
        watcher = asyncio.ensure_future(_until_disconnected(request))
        try:
            await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if not task.done():
                # The client went away (cancelled or timed out): cancelling kills the command in the VM.
                task.cancel()
                await asyncio.wait({task})
                return Response(status_code=CLIENT_CLOSED_REQUEST)
            if task.cancelled():
                raise WorkspaceUnavailableError(f"sandbox {sandbox_id!r} was deleted while the command ran")
            result = task.result()
            return RunResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)  # type: ignore[attr-defined]
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as error:
            return _error_response(error)
        finally:
            watcher.cancel()
            entry.inflight.discard(task)

    @app.delete(API + "/{sandbox_id}", status_code=204)
    async def delete(sandbox_id: str) -> Response:
        await registry.destroy(sandbox_id)
        return Response(status_code=204)

    return app


SD_LISTEN_FDS_START = 3


def systemd_socket() -> socket.socket | None:
    """The listening socket systemd passed us (owner, group and mode set by the .socket unit), if any."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        return None
    return socket.socket(fileno=SD_LISTEN_FDS_START)


def bind_socket(path: str, group: str | None) -> socket.socket:
    """Bind the listening socket ourselves, so it is `0660` and group-owned before anyone can connect."""
    # A socket left by a crash blocks bind; anything else at that path is not ours to delete.
    with contextlib.suppress(FileNotFoundError):
        if stat.S_ISSOCK(os.lstat(path).st_mode):
            os.unlink(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)  # never world- or group-connectable, even for an instant
    try:
        sock.bind(path)
    finally:
        os.umask(old_umask)
    if group is not None:
        os.chown(path, -1, grp.getgrnam(group).gr_gid)
    os.chmod(path, 0o660)
    return sock


def main() -> None:
    import uvicorn

    from autoreviewer.sandbox.providers import SmolProvider

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", help="unix socket path (not needed under systemd socket activation)")
    parser.add_argument("--socket-group", help="group allowed to connect; this is the access control")
    parser.add_argument("--max-sandboxes", type=int, default=Limits.max_sandboxes)
    parser.add_argument("--max-age", type=float, default=Limits.max_age_s, help="seconds before a sandbox is reaped")
    parser.add_argument("--max-cpus", type=int, default=Limits.max_cpus)
    parser.add_argument("--max-memory-mb", type=int, default=Limits.max_memory_mb)
    parser.add_argument("--permit-host", action="append", default=[], help="host a sandbox may reach (repeatable)")
    parser.add_argument("--permit-image", action="append", default=None, help="image a sandbox may boot (repeatable)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    limits = Limits(
        max_sandboxes=args.max_sandboxes,
        max_age_s=args.max_age,
        max_cpus=args.max_cpus,
        max_memory_mb=args.max_memory_mb,
        permitted_hosts=frozenset(SandboxSpec(allow_hosts=args.permit_host).allow_hosts),
        permitted_images=frozenset(args.permit_image) if args.permit_image else None,
    )
    if (sock := systemd_socket()) is None:
        if not args.socket:
            parser.error("--socket is required unless started by systemd socket activation")
        sock = bind_socket(args.socket, args.socket_group)
    uvicorn.run(create_app(SmolProvider(), limits), fd=sock.fileno(), log_level="warning")


if __name__ == "__main__":
    main()
