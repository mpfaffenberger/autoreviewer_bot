"""The gateway deployment's model facts, loaded once from its profile (deployment_profile.py).

No model is named here. Edit deployment_profile.json, or point PRISM_DEPLOYMENT_PROFILE at a replacement.
DESIGN.md §7 ("Lanes") has the measurements behind the reference values.
"""

from typing import get_args

from autoreviewer.deployment_profile import ReasoningEffort as ReasoningEffort
from autoreviewer.deployment_profile import load_deployment_profile

_profile = load_deployment_profile()
DEFAULT_BASE_URL = _profile.base_url
CONTEXT_WINDOWS = {model.id: model.context_window for model in _profile.models.values()}
"""Every model an agent may be pinned to (config.models_from): an agent's context window follows its model."""
COMPACT_AT = {model.id: model.compact_at for model in _profile.models.values()}
"""The history, in tokens, above which an agent compacts its conversation (compaction.review_compaction): low
enough that the prompt plus REPLY_TOKENS always fits the window."""
DEFAULT_MODEL_LANES = {model.id: model.lanes for model in _profile.models.values()}
"""Requests at once per model, across every review."""
REPLY_TOKENS = _profile.reply_tokens
"""The longest reply any request may ask for (`max_tokens`)."""
DEFAULT_MODELS = {role: _profile.models[slot].id for role, slot in _profile.roles.items()}
"""Role -> model. PRISM_MODELS pins individual roles over it (config.models_from)."""
DEFAULT_REASONING = _profile.reasoning
DEFAULT_OVERFLOW_MODEL = _profile.overflow_model
REASONING_EFFORTS: tuple[ReasoningEffort, ...] = get_args(ReasoningEffort)


def default_compact_at(role: str) -> int:
    """Where the model the profile assigns to `role` compacts: the default for an agent built without a plan."""
    return COMPACT_AT[DEFAULT_MODELS[role]]
