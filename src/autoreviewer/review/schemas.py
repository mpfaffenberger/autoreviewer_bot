"""What the review models must return. Field descriptions are part of the prompt.

The scales follow published reviewer prompts: priorities from OpenAI Codex's review rubric, calibrated
confidence from Qodo PR-Agent. The lens reports everything it finds with honest scores; filtering is the
verifier's and the code's job, not the lens's.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

PRIORITY_HELP = (
    "0 (P0): drop everything to fix; breaks releases, operations, or major usage, without depending on unusual "
    "inputs. 1 (P1): urgent; wrong behaviour on realistic inputs, fix before merging. 2 (P2): real but narrower "
    "defect, fix eventually. 3 (P3): low; nice to have."
)
CONFIDENCE_HELP = (
    "How certain you are that this is a real defect introduced by this PR that matters in practice. 9-10: verified "
    "from the code you read, with a concrete triggering input. 7-8: very likely; depends only on reasonable "
    "assumptions about code you did not read. 5-6: plausible but unverified. 4 or less: a hunch."
)


class Finding(BaseModel):
    path: str = Field(description="File path in the repository, e.g. src/app/util.py")
    line: int = Field(description="New-file line number, as printed by the diff tools, on or next to the faulty code")
    priority: int = Field(ge=0, le=3, description=PRIORITY_HELP)
    confidence: int = Field(ge=0, le=10, description=CONFIDENCE_HELP)
    title: str = Field(description="At most 80 characters, imperative, naming the defect")
    explanation: str = Field(
        description="One paragraph: first the input, environment, or sequence that triggers it, then what goes "
        "wrong. Matter-of-fact; don't overstate."
    )
    suggestion: str | None = Field(default=None, description="A concrete fix, if there is an obvious one")
    kind: SkipJsonSchema[Literal["correctness", "types"]] = "correctness"
    """Which check found it. Not part of the model's schema: models only ever produce correctness findings."""


class LensReport(BaseModel):
    """Every field is required, so the whole report must arrive in one call. Qwen sometimes splits it
    across two output calls; with defaults, the partial first call validated and its empty findings won."""

    findings: list[Finding] = Field(
        description="Every qualifying defect you found, not just the first. Empty only if there are none.",
    )
    overall_correct: bool = Field(
        description="Whether the change as a whole is correct: existing behaviour and tests won't break, and it has no "
        "blocking defects. Ignore style and nits."
    )
    reviewed: list[str] = Field(
        description="The changed files you actually inspected (diff and surrounding code), so coverage is visible.",
    )


class Verification(BaseModel):
    confirmed: bool = Field(description="True only if you checked the code and the defect is real as described")
    reason: str = Field(description="One or two sentences: what you checked and what you found")
    priority: int | None = Field(
        default=None,
        ge=0,
        le=3,
        description="If confirmed and the reported priority is wrong, the right one (same scale). Otherwise omit.",
    )
    explanation: str | None = Field(
        default=None,
        description="If confirmed but the report gets a supporting detail wrong, a corrected explanation in the same "
        "style (the trigger first, then what goes wrong). Otherwise omit.",
    )
