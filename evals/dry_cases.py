"""Seeded reinvented wheels in this repo's own code: a PR re-implements a helper the repo already has, the
standard library, or a declared dependency. Each copy works (the tests still pass); it's just duplication.
Clean controls are the correctness suite's: they change code without duplicating anything.
"""

from __future__ import annotations

from evals.cases import CASES as CORRECTNESS_CASES
from evals.cases import Case

DRY_CORPUS = "d5e9888"

_PLUS_LINES = (
    "src/autoreviewer/review/lint.py",
    """    added = {c.path: added_lines(patches.get(c.path, "")) for c in changes if c.path.endswith(PYTHON_SUFFIXES)}""",  # noqa: E501
    """    def plus_lines(patch: str) -> frozenset[int]:
        numbers, line = set(), 0
        for raw in patch.splitlines():
            if raw.startswith("@@"):
                line = int(raw.split("+")[1].split(",")[0].split(" ")[0])
            elif raw.startswith("+"):
                numbers.add(line)
                line += 1
            elif not raw.startswith(("-", "\\\\")):
                line += 1
        return frozenset(numbers)

    added = {c.path: plus_lines(patches.get(c.path, "")) for c in changes if c.path.endswith(PYTHON_SUFFIXES)}""",
)

CASES: list[Case] = [
    Case(
        "dup_added_lines",
        *_PLUS_LINES,
        "Make type checks independent of the diff module",
        anchor="def plus_lines",
        corpus=DRY_CORPUS,
    ),
    Case(
        "dup_has_secret",
        "src/autoreviewer/review/simplify.py",
        "    if has_secret(suggestion.replacement):",
        """    if re.search(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_\\w{40,}|sk-[\\w-]{20,}|-----BEGIN", suggestion.replacement):""",  # noqa: E501
        "Check suggestions for secrets locally",
        also=(("import logging\n", "import logging\nimport re\n"),),
        corpus=DRY_CORPUS,
    ),
    Case(
        "dup_webhook_verify",
        "src/autoreviewer/web/app.py",
        """        if not signature.startswith("sha256=") or not verify(settings.webhook_secret, body, signature):""",
        """        expected = "sha256=" + hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):""",
        "Verify webhook signatures with the standard library",
        anchor="expected = ",
        also=(("import json\n", "import hashlib\nimport hmac\nimport json\n"),),
        corpus=DRY_CORPUS,
    ),
    Case(
        "dup_bounded_deque",
        "src/autoreviewer/review/progress.py",
        "        self.activity.appendleft(event)",
        "        self.activity.insert(0, event)\n        del self.activity[MAX_ACTIVITY:]",
        "Keep progress activity in a plain list",
        anchor="del self.activity[MAX_ACTIVITY:]",
        also=(
            (
                "    activity: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_ACTIVITY))",
                "    activity: list[str] = field(default_factory=list[str])",
            ),
        ),
        corpus=DRY_CORPUS,
    ),
    Case(
        "buried_dup_added_lines",
        *_PLUS_LINES,
        "Enforce type annotations with ruff, not a model",
        anchor="def plus_lines",
        host="d3f782d",  # the PR that added diff.added_lines: duplication within one PR
    ),
    *(case for case in CORRECTNESS_CASES if case.clean),
]
