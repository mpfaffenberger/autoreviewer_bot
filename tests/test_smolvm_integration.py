"""Real microVMs. Run with `uv run pytest -m smolvm`; needs /dev/kvm and the smolmachines engine.

Set SANDBOXD_SOCKET to run the egress checks against an installed daemon (as `smolrunner`, behind its
uid firewall) instead of an in-process one.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
from collections.abc import AsyncIterator
from typing import ClassVar

import pytest

from autoreviewer.sandbox.client import SandboxdBackend, SandboxdClient
from autoreviewer.sandbox.daemon import Limits
from autoreviewer.sandbox.protocol import SandboxSpec
from autoreviewer.sandbox.providers import SandboxProvider, SmolProvider
from tests import test_sandbox_conformance as base  # module import: a bare class import would be collected twice
from tests.conftest import running_daemon

# An installed daemon only permits digest-pinned images, so point this at one of them.
PYTHON_IMAGE = os.environ.get("SANDBOXD_TEST_IMAGE", "python:3.12-slim")
PYPI = ["pypi.org", "files.pythonhosted.org"]

# Every destination here must be unreachable from a sandbox, whatever its allowlist.
FORBIDDEN = {
    "example.com (not allowlisted)": ("example.com", 443),
    "raw public IP": ("1.1.1.1", 443),
    "DeepSeek box on the LAN": ("192.168.1.46", 8888),
    "router": ("192.168.1.1", 80),
    "cloud metadata": ("169.254.169.254", 80),
}

PROBE = r"""
import json, socket, sys
def tcp(host, port):
    try:
        socket.create_connection((host, port), timeout=5).close()
        return "OPEN"
    except Exception as error:
        return type(error).__name__
targets = json.loads(sys.argv[1])
gateway = None
for line in open("/proc/net/route").read().splitlines()[1:]:
    fields = line.split()
    if fields[1] == "00000000":
        gateway = socket.inet_ntoa(bytes.fromhex(fields[2])[::-1])
if gateway:
    targets["VM gateway (the host)"] = [gateway, 8484]
print(json.dumps({name: tcp(host, port) for name, (host, port) in targets.items()}))
"""


def _host_lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("1.1.1.1", 53))  # no packet is sent; this just picks the outbound interface
        return probe.getsockname()[0]


class TestSmolSandboxdConformance(base.TestSandboxdConformance):
    pytestmark: ClassVar[list[pytest.MarkDecorator]] = [pytest.mark.anyio, pytest.mark.smolvm]

    @pytest.fixture
    def provider(self) -> SandboxProvider:
        return SmolProvider()

    @pytest.fixture
    def spec(self) -> SandboxSpec:
        return SandboxSpec()  # smolvm's own Alpine: no image pull, no network


@contextlib.asynccontextmanager
async def _client() -> AsyncIterator[SandboxdClient]:
    if installed := os.environ.get("SANDBOXD_SOCKET"):
        async with SandboxdClient(installed) as client:
            yield client
    else:
        async with running_daemon(SmolProvider(), Limits(permitted_hosts=frozenset(PYPI))) as client:
            yield client


async def _probe(backend: SandboxdBackend, targets: dict[str, tuple[str, int]]) -> dict[str, str]:
    result = await backend.run(["python3", "-c", PROBE, json.dumps(targets)], timeout=120)
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.smolvm
@pytest.mark.anyio
async def test_allowlisted_sandbox_reaches_only_its_hosts() -> None:
    targets = {"pypi.org": ("pypi.org", 443), **FORBIDDEN, "Boodleton on the host LAN IP": (_host_lan_ip(), 8484)}
    async with _client() as client:
        backend = SandboxdBackend(client, spec=SandboxSpec(image=PYTHON_IMAGE, allow_hosts=PYPI))
        try:
            reached = await _probe(backend, targets)
            install = await backend.run(
                ["pip", "install", "--no-cache-dir", "--root-user-action=ignore", "six"], timeout=180
            )
        finally:
            await backend.destroy()
    assert reached.pop("pypi.org") == "OPEN"
    assert {name: outcome for name, outcome in reached.items() if outcome == "OPEN"} == {}
    assert install.exit_code == 0, install.stderr


@pytest.mark.smolvm
@pytest.mark.anyio
async def test_sandbox_without_allowlist_has_no_network() -> None:
    async with _client() as client:
        backend = SandboxdBackend(client, spec=SandboxSpec(image=PYTHON_IMAGE))
        try:
            reached = await _probe(backend, {"pypi.org": ("pypi.org", 443), **FORBIDDEN})
        finally:
            await backend.destroy()
    assert {name: outcome for name, outcome in reached.items() if outcome == "OPEN"} == {}
