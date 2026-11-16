"""The daemon's own rules, on top of the backend contract: ceilings, capacity, reaping, wire errors."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError
from pydantic_ai.workspaces import (
    WorkspaceError,
    WorkspaceOutputLimitError,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

from autoreviewer.sandbox.client import SandboxdBackend
from autoreviewer.sandbox.daemon import Limits
from autoreviewer.sandbox.protocol import (
    CapacityError,
    ForbiddenSpecError,
    SandboxSpec,
    decode_error,
    encode_error,
)
from autoreviewer.sandbox.providers import ProvidedSandbox
from tests.conftest import LocalDirProvider, running_daemon

pytestmark = pytest.mark.anyio

PYPI = ["pypi.org", "files.pythonhosted.org"]


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (SandboxSpec(allow_hosts=["example.com"]), "hosts not permitted: example.com"),
        (SandboxSpec(cpus=64), "cpus=64 is above the limit"),
        (SandboxSpec(memory_mb=65536), "memory_mb=65536 is above the limit"),
        (SandboxSpec(image="evil:latest"), "image not permitted: 'evil:latest'"),
    ],
)
async def test_specs_above_the_ceilings_are_refused(spec: SandboxSpec, message: str) -> None:
    limits = Limits(permitted_hosts=frozenset(PYPI), permitted_images=frozenset({None, "python:3.12-slim"}))
    async with running_daemon(LocalDirProvider(), limits) as client:
        with pytest.raises(ForbiddenSpecError, match=message):
            await client.create(spec)


async def test_permitted_hosts_are_accepted() -> None:
    async with running_daemon(LocalDirProvider(), Limits(permitted_hosts=frozenset(PYPI))) as client:
        assert await client.create(SandboxSpec(allow_hosts=PYPI))


@pytest.mark.parametrize("host", ["*.example.com", "1.1.1.1", "pypi.org:443", "https://pypi.org", "localhost", ""])
def test_allow_hosts_only_takes_plain_hostnames(host: str) -> None:
    with pytest.raises(ValidationError):
        SandboxSpec(allow_hosts=[host])


async def test_capacity_is_enforced_and_freed_on_destroy() -> None:
    async with running_daemon(LocalDirProvider(), Limits(max_sandboxes=1)) as client:
        first = await client.create(SandboxSpec())
        with pytest.raises(CapacityError):
            await client.create(SandboxSpec())
        await client.destroy(first)
        assert await client.create(SandboxSpec())


async def test_old_sandboxes_are_reaped() -> None:
    async with running_daemon(LocalDirProvider(), Limits(max_age_s=0.2), reap_interval_s=0.1) as client:
        backend = SandboxdBackend(client, spec=SandboxSpec())
        assert (await backend.run(["true"])).exit_code == 0
        await asyncio.sleep(0.6)
        with pytest.raises(WorkspaceUnavailableError, match="does not exist"):
            await backend.run(["true"])


async def test_an_unreachable_daemon_is_a_workspace_error() -> None:
    from autoreviewer.sandbox.client import SandboxdClient

    async with SandboxdClient("/tmp/definitely-not-a-sandboxd.sock") as client:
        with pytest.raises(WorkspaceError, match="unreachable"):
            await client.create(SandboxSpec())


def test_refs_from_other_providers_are_rejected() -> None:
    from pydantic_ai.workspaces import WorkspaceRef

    from autoreviewer.sandbox.client import SandboxdClient

    with pytest.raises(ValueError, match="not"):
        SandboxdBackend(SandboxdClient("/tmp/x.sock"), ref=WorkspaceRef(provider="smol", id="pydantic-ai-abc"))


@pytest.mark.parametrize(
    "error",
    [
        WorkspaceTimeoutError("slow", stdout="partial out", stderr="partial err"),
        WorkspaceOutputLimitError("chatty", limit=10, stdout="o", stderr="e"),
        WorkspaceUnavailableError("gone"),
        CapacityError("full"),
        WorkspaceError("boom"),
        ForbiddenSpecError("no"),
        TypeError("wrong form"),
        ValueError("bad value"),
    ],
)
def test_errors_survive_the_wire(error: Exception) -> None:
    body = encode_error(error)
    assert body is not None
    rebuilt = decode_error(type(body).model_validate_json(body.model_dump_json()))
    assert type(rebuilt) is type(error)
    assert str(rebuilt) == str(error)
    for attribute in ("stdout", "stderr", "limit"):
        assert getattr(rebuilt, attribute, None) == getattr(error, attribute, None)


def test_unknown_errors_are_not_encoded() -> None:
    assert encode_error(KeyError("daemon bug")) is None


def test_socket_is_group_only(tmp_path_factory: pytest.TempPathFactory) -> None:
    import grp
    import os
    import stat
    import tempfile

    from autoreviewer.sandbox.daemon import bind_socket

    path = f"{tempfile.mkdtemp(prefix='sbxs-', dir='/tmp')}/s.sock"
    group = grp.getgrgid(os.getgid()).gr_name
    sock = bind_socket(path, group)
    try:
        info = os.stat(path)
        assert stat.S_ISSOCK(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o660
        assert info.st_gid == os.getgid()
        sock.close()
        bind_socket(path, None).close()  # a stale socket from a crash is replaced, not an error
    finally:
        os.unlink(path)


async def test_provider_failures_are_typed_and_free_their_slot() -> None:
    class BrokenProvider:
        async def create(self, spec: SandboxSpec) -> ProvidedSandbox:
            raise RuntimeError("engine exploded")

    async with running_daemon(BrokenProvider(), Limits(max_sandboxes=1)) as client:
        for _ in range(2):  # the second attempt proves the failed one didn't keep its capacity slot
            with pytest.raises(WorkspaceError, match="could not create the sandbox: engine exploded"):
                await client.create(SandboxSpec())
