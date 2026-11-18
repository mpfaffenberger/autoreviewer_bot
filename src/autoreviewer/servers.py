"""Facts about the model servers on chaos-emerald. Change them here when a server is reconfigured."""

from __future__ import annotations

QWEN_CONTEXT_WINDOW = 229_376
"""Qwen runs 4 streams of 224k tokens (TensorFold `PARALLEL=4 CONTEXT=229376`, the engine's 0.5 GiB reserve): four
full 262k windows don't fit the card, and the longest prompt in the day before the change was 216k (2026-11-18)."""
DEEPSEEK_CONTEXT_WINDOW = 1_048_576
"""As reported by its serving engine's /v1/models (max_model_len), 2026-11-16."""
DEFAULT_MODEL_LANES = {"qwen3.8-flash-next": 4, "deepseek-v4.1-flash": 4}
"""Requests at once per model, across every review: Qwen's stream count, and DeepSeek's share of a shared server."""
