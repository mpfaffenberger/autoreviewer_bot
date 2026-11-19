"""Facts about the model servers on chaos-emerald. Change them here when a server is reconfigured; DESIGN.md §7
("Lanes") has the measurements behind them."""

QWEN = "qwen3.8-flash-next"
DEEPSEEK = "deepseek-v4.1-flash"
KIMI = "kimi-k3.1-pro"
"""Moonshot's hosted coding plan, proxied by Boodleton (not on chaos-emerald's GPUs): mind its rate limits."""
QWEN_CONTEXT_WINDOW = 229_376
"""4 streams of 224k tokens on Qwen's server (DESIGN.md §7)."""
DEEPSEEK_CONTEXT_WINDOW = 1_048_576
"""As reported by its serving engine's /v1/models (max_model_len), 2026-11-16."""
KIMI_CONTEXT_WINDOW = 262_144
"""As Boodleton's /v1/models lists it (2026-11-18); Boodleton lists every model this way, so it's a floor."""
CONTEXT_WINDOWS = {QWEN: QWEN_CONTEXT_WINDOW, DEEPSEEK: DEEPSEEK_CONTEXT_WINDOW, KIMI: KIMI_CONTEXT_WINDOW}
"""Every model an agent may be pinned to (config.models_from): an agent's context window follows its model."""
DEFAULT_MODEL_LANES = {QWEN: 4, DEEPSEEK: 4, KIMI: 2}
"""Requests at once per model, across every review: Qwen's stream count, DeepSeek's share of a shared server,
and few to Kimi's hosted API (it only checks findings)."""
