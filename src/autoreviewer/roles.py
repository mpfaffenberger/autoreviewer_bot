"""The agent roles a deployment assigns models to. Which model plays a role is configuration, never code."""

from typing import Literal, get_args

AgentKind = Literal["correctness", "security", "dry", "docs", "tests", "simplify"]
"""Each kind has a finder (the role named after it) and a checker (`<kind>.verifier`)."""
AGENT_KINDS: tuple[AgentKind, ...] = get_args(AgentKind)


def verifier_role(kind: AgentKind) -> str:
    """The role of the agent that checks `kind`'s findings: the one place the `<kind>.verifier` name is made."""
    return f"{kind}.verifier"


CONVERSATION = "conversation"
"""The role that answers replies under the bot's findings (review/conversation.py)."""
AGENT_ROLES = (*AGENT_KINDS, *map(verifier_role, AGENT_KINDS), CONVERSATION)
