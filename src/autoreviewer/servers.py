"""Facts about the model servers on chaos-emerald. Change them here when a server is reconfigured; DESIGN.md §7
("Lanes") has the measurements behind them."""

from typing import Literal, get_args

QWEN = "qwen3.8-flash-next"
DEEPSEEK = "deepseek-v4.1-flash"
QWEN_CONTEXT_WINDOW = 229_376
"""4 streams of 224k tokens on Qwen's server (DESIGN.md §7)."""
DEEPSEEK_CONTEXT_WINDOW = 1_048_576
"""As reported by its serving engine's /v1/models (max_model_len), 2026-11-16."""
CONTEXT_WINDOWS = {QWEN: QWEN_CONTEXT_WINDOW, DEEPSEEK: DEEPSEEK_CONTEXT_WINDOW}
"""Every model an agent may be pinned to (config.models_from): an agent's context window follows its model.

Real names only. Boodleton also serves aliases: `qwen3.8-27b` is this Qwen and `glm53` is this DeepSeek. They
stay out because the rule that a finder and its checker differ, and the per-model lanes, compare names, so a
pin to an alias stops startup."""
DEFAULT_MODEL_LANES = {QWEN: 4, DEEPSEEK: 4}
"""Requests at once per model, across every review: Qwen's stream count, and DeepSeek's share of a shared server."""

ReasoningEffort = Literal["low", "medium", "high"]
"""Efforts a request may ask for. DeepSeek thinks at any of them and not at all without one (2026-11-18: 43-59
reasoning tokens at each level on the same question, 0 without); Qwen thinks by default."""
REASONING_EFFORTS: tuple[ReasoningEffort, ...] = get_args(ReasoningEffort)
