"""What the review models must return. Field descriptions are part of the prompt."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["high", "medium"]
"""No "low": every comment blocks the merge, so nits are never reported."""


class Finding(BaseModel):
    path: str = Field(description="File path exactly as in the diff header, e.g. src/app/util.py")
    line: int = Field(description="Line number in the NEW file, as printed in the left column of the annotated diff")
    severity: Severity = Field(
        description="high: wrong result or crash on normal input. medium: wrong only for a specific, realistic input"
    )
    title: str = Field(description="One sentence naming the bug")
    explanation: str = Field(description="The input or sequence that triggers it, and what goes wrong as a result")
    suggestion: str | None = Field(default=None, description="A concrete fix, if there is an obvious one")


class LensReport(BaseModel):
    findings: list[Finding] = Field(
        default_factory=list[Finding], description="Real bugs only. An empty list is a good, common outcome."
    )


class Verification(BaseModel):
    confirmed: bool = Field(description="True only if you checked the code and the bug is real as described")
    reason: str = Field(description="One or two sentences: what you checked and what you found")
