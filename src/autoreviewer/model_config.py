"""Portable model selection. Only load operator-owned configuration: factories execute Python."""

from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from pydantic_ai.models import Model


class PrismConfig(BaseModel):
    """Per-model options are keyed by the full finder/checker spec, not the role."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    finder: str
    checker: str
    lanes: dict[str, Annotated[int, Field(ge=1)]] = Field(default_factory=dict)
    compact_at: dict[str, Annotated[int, Field(ge=8)]] = Field(default_factory=dict)

    @field_validator("finder", "checker")
    @classmethod
    def model_spec(cls, spec: str) -> str:
        provider, sep, name = spec.partition(":")
        if not sep or not provider or not name or any(c.isspace() for c in spec):
            raise ValueError("expected provider:model or factory:module:function")
        if provider == "factory":
            module, _, function = name.partition(":")
            if not function.isidentifier() or not all(part.isidentifier() for part in module.split(".")):
                raise ValueError("expected factory:module:function")
        return spec

    @model_validator(mode="after")
    def check_models(self) -> Self:
        if self.finder == self.checker:
            raise ValueError("finder and checker must use different models")
        unknown = (self.lanes.keys() | self.compact_at.keys()) - {self.finder, self.checker}
        if unknown:
            raise ValueError(f"options for unconfigured models: {sorted(unknown)}")
        return self

    @classmethod
    def from_file(cls, path: str | Path) -> Self:
        return cls.model_validate_json(Path(path).read_bytes())

    def lane_count(self, spec: str) -> int:
        return self.lanes.get(spec, 1)

    def resolve(self, spec: str) -> tuple[Model, int]:
        from pydantic_ai.models import Model, infer_model

        from autoreviewer.review.agents import with_lanes

        if spec.startswith("factory:"):
            _, module, function = spec.split(":")
            model = getattr(import_module(module), function)()
            if not isinstance(model, Model):
                raise TypeError(f"{spec} must return a pydantic_ai Model")
        elif spec.startswith("claude-code:") and not (
            "PRISM_CLAUDE_CODE_BASE_URL" in os.environ or "PRISM_CLAUDE_CODE_API_KEY" in os.environ
        ):
            try:
                from pydantic_ai_claude_code import ClaudeCodeModel
            except ModuleNotFoundError as exc:
                if exc.name != "pydantic_ai_claude_code":
                    raise
                raise ValueError(
                    "Claude Code needs the claude-code extra: uv sync --extra claude-code (Python 3.13-3.14)"
                ) from exc
            model = ClaudeCodeModel(spec.split(":", 1)[1])
        elif spec.startswith("claude-code:"):
            # Explicit opt-in to a model-only, operator-provisioned Chat Completions endpoint.
            # Never launch a coding CLI or inherit an unrelated OpenAI credential.
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            url = os.environ.get("PRISM_CLAUDE_CODE_BASE_URL")
            key = os.environ.get("PRISM_CLAUDE_CODE_API_KEY")
            if not url or not key:
                raise ValueError("claude-code requires PRISM_CLAUDE_CODE_BASE_URL and PRISM_CLAUDE_CODE_API_KEY")
            model = OpenAIChatModel(spec.split(":", 1)[1], provider=OpenAIProvider(base_url=url, api_key=key))
        else:
            model = infer_model(spec)

        # Leave at least half of a known window for output, tool schemas and counting error.
        # For unknown models use a small default; the operator can supply a measured target.
        window = model.profile.get("context_window")
        limit = None if window is None else min(window // 2, window - ((model.settings or {}).get("max_tokens") or 0))
        target = self.compact_at.get(spec, limit if limit is not None else 4096)
        if target < 8 or (limit is not None and target > limit):
            raise ValueError(f"compact_at={target} for {spec} leaves insufficient room in its {window}-token window")
        return with_lanes(model, spec, self.lane_count(spec)), target
