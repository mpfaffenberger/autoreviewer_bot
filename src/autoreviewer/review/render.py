"""Turn findings into GitHub markdown. All model text passes through `sanitize` before it is posted."""

from __future__ import annotations

import re

from autoreviewer.review.schemas import Finding

MAX_TEXT = 3000

_MENTION = re.compile(r"(?<![\w`])@(?=[A-Za-z0-9])")
_SECRETS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|sk_bood_[A-Za-z0-9_-]{8,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
)


def sanitize(text: str) -> str:
    """Make model text safe to post: no pings, no secrets echoed back, bounded length."""
    for pattern in _SECRETS:
        text = pattern.sub("[redacted]", text)
    text = _MENTION.sub("@\u200b", text)  # a zero-width space after "@" stops GitHub notifying anyone
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def comment_body(finding: Finding) -> str:
    parts = [f"**{finding.title}** ({finding.severity}, correctness)", finding.explanation]
    if finding.suggestion:
        parts.append(f"**Suggested fix:** {finding.suggestion}")
    return sanitize("\n\n".join(parts))


def review_body(inline: list[Finding], general: list[Finding], *, footer: str) -> str:
    if not inline and not general:
        return f"No correctness issues found.\n\n{footer}"
    count = len(inline) + len(general)
    lines = [f"Found {count} correctness issue{'s' if count != 1 else ''} to fix before merging."]
    if general:
        lines.append("\nOutside the changed lines:")
        lines.extend(f"- `{f.path}:{f.line}`: {comment_body(f)}" for f in general)
    lines.append(f"\n{footer}")
    return "\n".join(lines)
