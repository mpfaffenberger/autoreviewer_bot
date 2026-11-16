"""Where sandboxes come from. The daemon only sees `SandboxProvider`; only this module knows about smol.

The production provider is a thin layer over the harness's `SmolSandboxBackend`: we create the VM
ourselves, because only the smol SDK (not `SmolSandbox`) can scope egress to a host allowlist
(pydantic/pydantic-ai#11601), then hand it to `SmolSandboxBackend(sandbox=...)`, which owns the hard
part (literal argv, exit 127, timeouts that kill the process tree, bounded output, gone-VM detection).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic_ai.workspaces import SupportsCommands, WorkspaceBackend

from autoreviewer.sandbox.protocol import SandboxSpec


class CommandBackend(WorkspaceBackend, SupportsCommands, Protocol):
    """A workspace backend that can run commands; file operations derive from its shell."""


@dataclass(frozen=True, slots=True)
class ProvidedSandbox:
    backend: CommandBackend
    destroy: Callable[[], Awaitable[None]]
    """Delete the environment. Must be idempotent and must not raise if it is already gone."""


class SandboxProvider(Protocol):
    async def create(self, spec: SandboxSpec) -> ProvidedSandbox: ...


NAME_PREFIX = "autoreviewer-"
LABELS = {"app": "autoreviewer-sandboxd"}


class SmolProvider:
    """One local smolvm microVM per sandbox, egress limited to `spec.allow_hosts` (none if empty)."""

    async def create(self, spec: SandboxSpec) -> ProvidedSandbox:
        # Imported here so the daemon's tests (and the client) never need the smol engine or /dev/kvm.
        import smol
        from pydantic_ai_harness.smol_sandbox import SmolSandboxBackend

        config = smol.MachineConfig(
            name=f"{NAME_PREFIX}{uuid.uuid4().hex}",
            image=spec.image,
            labels=dict(LABELS),
            # Not persistent, not detached: every VM dies with this process, so a crash leaves no orphans.
            resources=smol.ResourceSpec(
                cpus=spec.cpus,
                memory_mb=spec.memory_mb,
                # `None`, not `[]`: an empty list must never be read as "networking on, nothing listed".
                allow_hosts=list(spec.allow_hosts) or None,
            ),
        )
        # Pinned to the local target, as the harness does, so a stray SMOL_CLOUD_TOKEN can't move us to Smol Cloud.
        machine = await smol.AsyncMachine.create(config, smol.ConnectOptions(target="local"))

        async def destroy() -> None:
            try:
                await machine.delete()
            except smol.SmolError as error:
                if error.code != "NOT_FOUND":
                    raise

        return ProvidedSandbox(
            backend=SmolSandboxBackend(sandbox=machine, working_dir=spec.working_dir), destroy=destroy
        )
