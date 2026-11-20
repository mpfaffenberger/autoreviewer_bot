"""Facts about the model servers on chaos-emerald. Change them here when a server is reconfigured; DESIGN.md §7
("Lanes") has the measurements behind them."""

from typing import Literal, get_args

QWEN = "qwen3.8-flash-next"
DEEPSEEK = "deepseek-v4.1-flash"
QWEN_CONTEXT_WINDOW = 172_032
"""5 streams of 168k tokens on Qwen's server (DESIGN.md §7); it was 4 of 224k until Nov 20."""
DEEPSEEK_CONTEXT_WINDOW = 1_048_576
"""As reported by its serving engine's /v1/models (max_model_len), 2026-11-16."""
CONTEXT_WINDOWS = {QWEN: QWEN_CONTEXT_WINDOW, DEEPSEEK: DEEPSEEK_CONTEXT_WINDOW}
"""Every model an agent may be pinned to (config.models_from): an agent's context window follows its model.

Real names only. Boodleton also serves aliases: `qwen3.8-27b` is this Qwen and `glm53` is this DeepSeek. They
stay out because the rule that a finder and its checker differ, and the per-model lanes, compare names, so a
pin to an alias stops startup."""
REPLY_TOKENS = 16_384
"""The longest reply any request may ask for (`max_tokens`). Qwen's server reserves it in the window before it
accepts a prompt, and reserves 32,768 for a request that doesn't say. Of 18,632 Qwen replies to the bot by
Nov 20, the longest was 13,090 tokens (99.9% under 9,335); DeepSeek's, 6,279."""
COMPACT_AT = {QWEN: 140_000, DEEPSEEK: int(DEEPSEEK_CONTEXT_WINDOW * 0.6)}
"""The history, in tokens, above which an agent compacts its conversation (compaction.review_compaction): low
enough that the prompt plus REPLY_TOKENS always fits the window. Qwen's is Mike's 140k (Nov 20), well above 60%
of its window, so its reviewers compact less often; DeepSeek keeps 60% of its long window."""
DEFAULT_MODEL_LANES = {QWEN: 5, DEEPSEEK: 4}
"""Requests at once per model, across every review: Qwen's stream count, and DeepSeek's share of a shared server."""

ReasoningEffort = Literal["low", "medium", "high"]
"""Efforts a request may ask for. DeepSeek thinks at any of them and not at all without one (2026-11-18: 43-59
reasoning tokens at each level on the same question, 0 without); Qwen thinks by default."""
REASONING_EFFORTS: tuple[ReasoningEffort, ...] = get_args(ReasoningEffort)
