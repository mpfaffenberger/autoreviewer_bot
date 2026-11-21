"""The gateway deployment profile: which models the built-in review plan runs on and how each is tuned.

It applies when PRISM_CONFIG is unset. The packaged deployment_profile.json describes the reference
deployment. PRISM_DEPLOYMENT_PROFILE names an operator-owned replacement, read once at startup, so a
model, its tuning, or the overflow model changes without editing Python or rebuilding the image.
"""

import os
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator, model_validator

from autoreviewer.model_config import check_model_spec
from autoreviewer.roles import AGENT_KINDS, AGENT_ROLES, verifier_role

ReasoningEffort = Literal["low", "medium", "high"]
"""Efforts a request may ask for. Whether a model reasons without one is its server's choice."""


class ProfileSection(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class GatewayModel(ProfileSection):
    """One model the gateway serves. Use its real name, not an alias: lanes and the rule that a finder and
    its checker differ both compare names."""

    id: Annotated[str, Field(pattern=r"^\S+$")]
    context_window: PositiveInt
    compact_at: PositiveInt
    """History tokens above which an agent compacts; with `reply_tokens` it must fit the window."""
    lanes: PositiveInt
    """Requests at once to this model, across every review."""


class DeploymentProfile(ProfileSection):
    base_url: Annotated[str, Field(pattern=r"^https?://\S+$")]
    """The gateway's OpenAI-compatible base URL, API prefix included. BOODLETON_BASE_URL overrides it."""
    models: dict[str, GatewayModel]
    """Slot name -> model. Slot names are the operator's own; `roles` refers to them."""
    roles: dict[str, str]
    """Agent role -> slot, for every role in roles.AGENT_ROLES. PRISM_MODELS pins roles over it."""
    reply_tokens: PositiveInt
    """The longest reply any request may ask for (`max_tokens`)."""
    reasoning: dict[str, ReasoningEffort] = Field(default_factory=dict)
    """Role -> reasoning effort. Unlisted roles send none. PRISM_REASONING overrides it."""
    overflow_model: str | None = None
    """Model spec (model_config.check_model_spec) whose finders take reviews when the normal workers are busy;
    None for no overflow. Not a `local:` spec: the profile has no endpoint to give it."""

    @field_validator("overflow_model")
    @classmethod
    def overflow_spec(cls, spec: str | None) -> str | None:
        if spec is None:
            return None
        if check_model_spec(spec).startswith("local:"):
            raise ValueError("overflow_model cannot be a local: spec; the profile has no endpoint for it")
        return spec

    @model_validator(mode="after")
    def check_references(self) -> Self:
        if len({model.id for model in self.models.values()}) != len(self.models):
            raise ValueError("gateway model IDs must be distinct")
        for slot, model in self.models.items():
            if model.compact_at + self.reply_tokens > model.context_window:
                raise ValueError(f"{slot}: compaction threshold plus reply budget exceeds context window")
        if self.roles.keys() != set(AGENT_ROLES) or not self.reasoning.keys() <= self.roles.keys():
            raise ValueError(f"roles must cover {', '.join(AGENT_ROLES)}; reasoning may use a subset")
        if unknown := set(self.roles.values()) - self.models.keys():
            raise ValueError(f"roles reference unknown model slots: {sorted(unknown)}")
        for kind in AGENT_KINDS:
            if self.roles[kind] == self.roles[verifier_role(kind)]:
                raise ValueError(f"{kind} and its verifier must use different models")
        return self


def load_deployment_profile() -> DeploymentProfile:
    """The profile PRISM_DEPLOYMENT_PROFILE names, else the packaged one. A bad override stops startup
    rather than falling back to models the operator didn't choose."""
    override = os.environ.get("PRISM_DEPLOYMENT_PROFILE", "").strip()
    source = Path(override) if override else files("autoreviewer").joinpath("deployment_profile.json")
    try:
        return DeploymentProfile.model_validate_json(source.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError(f"Invalid deployment profile {source}: {exc}") from exc
