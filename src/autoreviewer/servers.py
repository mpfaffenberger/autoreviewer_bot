"""Facts about the model servers on chaos-emerald. Change them here when a server is reconfigured; DESIGN.md §7
("Lanes") has the measurements behind them."""

QWEN = "qwen3.8-flash-next"
DEEPSEEK = "deepseek-v4.1-flash"
QWEN_CONTEXT_WINDOW = 229_376
"""4 streams of 224k tokens on Qwen's server (DESIGN.md §7)."""
DEEPSEEK_CONTEXT_WINDOW = 1_048_576
"""As reported by its serving engine's /v1/models (max_model_len), 2026-11-16."""
DEFAULT_MODEL_LANES = {QWEN: 4, DEEPSEEK: 4}
"""Requests at once per model, across every review: Qwen's stream count, and DeepSeek's share of a shared server."""
