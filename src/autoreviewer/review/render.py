"""Turn findings into GitHub markdown. All model text passes through `sanitize` before it is posted."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from autoreviewer.review.schemas import Finding

if TYPE_CHECKING:  # pipeline imports progress, which imports this module
    from autoreviewer.review.pipeline import Outcome

MAX_TEXT = 3000

_MENTION = re.compile(r"(?<![\w`])@(?=[A-Za-z0-9])")
_SECRETS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|sk_bood_[A-Za-z0-9_-]{8,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
)


def has_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRETS)


def sanitize(text: str, *, limit: int = MAX_TEXT) -> str:
    """Make model text safe to post: no pings, no secrets echoed back, bounded length."""
    for pattern in _SECRETS:
        text = pattern.sub("[redacted]", text)
    text = _MENTION.sub("@\u200b", text)  # a zero-width space after "@" stops GitHub notifying anyone
    return text if len(text) <= limit else text[: limit - 1] + "…"


_POSTED_TITLE = re.compile(r"\A\*\*\[P[0-3]\] ([^\n]*)\*\* \(")


def posted_title(body: str) -> str | None:
    """The title of a finding as comment_body wrote it, or None for a body it didn't write (a simplification's
    has no priority)."""
    return match[1] if (match := _POSTED_TITLE.match(body)) else None


def comment_body(finding: Finding, *, blocking: bool = True) -> str:
    gate = "" if blocking else ", non-blocking"
    rank = "" if finding.kind == "simplify" else f"[P{finding.priority}] "
    parts = [f"**{rank}{finding.title}** ({finding.kind}{gate})", finding.explanation]
    if finding.suggestion:
        parts.append(f"**Suggested fix:** {finding.suggestion}")
    body = sanitize("\n\n".join(parts))
    if finding.replacement is None:
        return body
    # Not sanitized: it's code the author applies with one click (a zero-width space after an "@" would
    # break a decorator), GitHub doesn't ping mentions inside code blocks, and suggestions with secrets
    # never get this far. The fence outgrows any backtick run inside.
    fence = "`" * max([3, *(len(run) + 1 for run in re.findall(r"`+", finding.replacement))])
    return f"{body}\n\n{fence}suggestion\n{finding.replacement}\n{fence}"


def review_body(outcome: Outcome, *, footer: str) -> str:
    """The review's summary: blocking findings first, advisory ones after, then where they are."""
    posted = outcome.posted
    cut_short = (
        f"**Partially reviewed:** {outcome.why_partial}, so it doesn't approve. Push again or comment a review "
        "request to retry.\n\n"
        if outcome.cut_short
        else ""
    )
    if not posted:
        return (
            f"{cut_short}No issues found in the part reviewed.\n\n{footer}"
            if cut_short
            else f"No issues found.\n\n{footer}"
        )
    blocking = [f for f in posted if outcome.is_blocking(f)]
    advisory = [f for f in posted if not outcome.is_blocking(f)]
    lines = [cut_short.rstrip()] if cut_short else []
    if blocking:
        lines.append(f"Found {_count(blocking)} to fix before merging.")
    simplify = [f for f in advisory if f.kind == "simplify"]
    advisory = [f for f in advisory if f.kind != "simplify"]
    if advisory:
        lines.append(f"{_count(advisory, 'advisory')} worth a look (non-blocking).")
    if simplify:
        cut = sum(f.lines_cut for f in simplify)
        share = f" ({cut / outcome.added_lines:.0%} of the {outcome.added_lines} added)" if outcome.added_lines else ""
        lines.append(f"{len(simplify)} optional one-click simplification(s), cutting {cut} line(s){share}.")
    if outcome.general:
        lines.append("\nOutside the changed lines:")
        lines.extend(
            f"- `{f.path}:{f.line}`: {comment_body(f, blocking=outcome.is_blocking(f))}" for f in outcome.general
        )
    lines.append(f"\n{footer}")
    return "\n".join(lines)


def _count(findings: list[Finding], kind: str = "") -> str:
    return f"{len(findings)} {kind + ' ' if kind else ''}issue{'s' if len(findings) != 1 else ''}"
