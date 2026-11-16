"""Wire format between the reviewer (client) and `sandboxd` (daemon).

One module owns both directions, so the client and the daemon can't drift apart: the request and
response models, and the codec that carries workspace exceptions across the socket with their type
and captured output intact.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field
from pydantic_ai.workspaces import (
    WorkspaceError,
    WorkspaceOutputLimitError,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

PROVIDER = "autoreviewer-sandboxd"
"""`WorkspaceRef.provider` for sandboxes this daemon hands out."""

API = "/v1/sandboxes"

# RFC 1123 hostname: dot-separated labels, no wildcards, no IPs-as-a-loophole for anything but names.
_HOSTNAME = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


def _hostname(value: str) -> str:
    value = value.lower()
    if not _HOSTNAME.fullmatch(value):
        raise ValueError(f"{value!r} is not a plain hostname (no wildcards, IPs, ports, or schemes)")
    return value


Hostname = Annotated[str, AfterValidator(_hostname)]


class SandboxSpec(BaseModel):
    """What a client asks for. The daemon clamps it to its own ceilings (see `daemon.Limits`)."""

    image: str | None = None
    """OCI image, ideally pinned by digest. `None` boots smolvm's own Alpine."""
    cpus: int = Field(default=2, ge=1)
    memory_mb: int = Field(default=2048, ge=256)
    allow_hosts: list[Hostname] = Field(default_factory=list[Hostname])
    """Egress allowlist. Empty means no network at all."""
    working_dir: str = "/workspace"


class Created(BaseModel):
    id: str


class WorkingDir(BaseModel):
    working_dir: str


class RunRequest(BaseModel):
    command: str | list[str]
    shell: bool = False
    env: dict[str, str] | None = None
    timeout: float | None = None


class RunResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


ErrorKind = Literal["unavailable", "timeout", "output_limit", "workspace", "type", "value", "capacity", "forbidden"]

_STATUS: dict[ErrorKind, int] = {
    "unavailable": 410,
    "timeout": 504,
    "output_limit": 507,
    "workspace": 500,
    "type": 400,
    "value": 422,
    "capacity": 429,
    "forbidden": 403,
}


class ErrorBody(BaseModel):
    kind: ErrorKind
    message: str
    stdout: str = ""
    stderr: str = ""
    limit: int | None = None

    @property
    def status(self) -> int:
        return _STATUS[self.kind]


class CapacityError(WorkspaceError):
    """The daemon is already running its maximum number of sandboxes. Retry later."""


class ForbiddenSpecError(ValueError):
    """The spec asks for more than the daemon permits (an image, host, or resource above its ceiling)."""


def encode_error(error: BaseException) -> ErrorBody | None:
    """Turn an exception into its wire form, or `None` if it isn't part of the contract (a daemon bug)."""
    # Most specific first: the timeout, output-limit and unavailable errors are all `WorkspaceError`s.
    match error:
        case WorkspaceTimeoutError():
            return ErrorBody(kind="timeout", message=str(error), stdout=error.stdout, stderr=error.stderr)
        case WorkspaceOutputLimitError():
            return ErrorBody(
                kind="output_limit", message=str(error), stdout=error.stdout, stderr=error.stderr, limit=error.limit
            )
        case WorkspaceUnavailableError():
            return ErrorBody(kind="unavailable", message=str(error))
        case CapacityError():
            return ErrorBody(kind="capacity", message=str(error))
        case WorkspaceError():
            return ErrorBody(kind="workspace", message=str(error))
        case ForbiddenSpecError():
            return ErrorBody(kind="forbidden", message=str(error))
        case TypeError():
            return ErrorBody(kind="type", message=str(error))
        case ValueError():
            return ErrorBody(kind="value", message=str(error))
        case _:
            return None


def decode_error(body: ErrorBody) -> Exception:
    """Rebuild the exception `encode_error` described, so callers see the same types as in-process."""
    match body.kind:
        case "timeout":
            return WorkspaceTimeoutError(body.message, stdout=body.stdout, stderr=body.stderr)
        case "output_limit":
            return WorkspaceOutputLimitError(
                body.message, limit=body.limit or 0, stdout=body.stdout, stderr=body.stderr
            )
        case "unavailable":
            return WorkspaceUnavailableError(body.message)
        case "capacity":
            return CapacityError(body.message)
        case "workspace":
            return WorkspaceError(body.message)
        case "forbidden":
            return ForbiddenSpecError(body.message)
        case "type":
            return TypeError(body.message)
        case "value":
            return ValueError(body.message)
