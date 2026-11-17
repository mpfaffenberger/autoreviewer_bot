"""Deliberately bloated versions of real functions from this repo, for measuring the simplify lens.

Each case replaces a tight function body (`old`) with a verbose one (`new`) that behaves identically:
narrating comments, single-use temporaries, if/else returning booleans, manual loops. The planted bloat is
`new`'s line count minus `old`'s; a perfect simplifier cuts all of it and the tests still pass. The clean
case changes behaviour-neutral tight code and should get no suggestions.
"""

from __future__ import annotations

from evals.cases import Case

SIMPLIFY_CORPUS = "eced320"
"""A snapshot that has every function below."""

CASES: list[Case] = [
    Case(
        "loop_and_narration",
        "src/autoreviewer/review/diff.py",
        """    return frozenset(line for line, raw in _new_side_lines(patch) if line and raw.startswith("+"))
""",
        """    result = set()
    # go through every line of the patch
    for line, raw in _new_side_lines(patch):
        # skip lines that have no line number
        if line is None or line == 0:
            continue
        # only added lines count
        if raw.startswith("+"):
            result.add(line)
    # return the result as a frozenset
    return frozenset(result)
""",
        "Make added_lines easier to follow",
        corpus=SIMPLIFY_CORPUS,
    ),
    Case(
        "temporaries_and_else",
        "src/autoreviewer/review/progress.py",
        """    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
""",
        """    words = text.split()
    joined = " ".join(words)
    length = len(joined)
    if length <= limit:
        return joined
    else:
        shortened = joined[: limit - 1]
        result = shortened + "…"
        return result
""",
        "Spell out the clipping steps",
        corpus=SIMPLIFY_CORPUS,
    ),
    Case(
        "boolean_ceremony",
        "src/autoreviewer/web/config.py",
        """    if value in _TRUE | _FALSE:
        return value in _TRUE
""",
        """    is_true = value in _TRUE
    is_false = value in _FALSE
    # check whether the value is one we understand
    if is_true or is_false:
        if is_true:
            return True
        else:
            return False
""",
        "Make parse_bool's accepted values explicit",
        corpus=SIMPLIFY_CORPUS,
    ),
    Case(
        "redundant_tuple",
        "src/autoreviewer/review/lint.py",
        """    return finding.priority, finding.path, finding.line
""",
        """    # sort by priority first, then by file, then by line
    priority = finding.priority
    path = finding.path
    line = finding.line
    key = (priority, path, line)
    return key
""",
        "Name the parts of the sort key",
        corpus=SIMPLIFY_CORPUS,
    ),
    Case(
        "clean_rename",
        "src/autoreviewer/review/progress.py",
        """    def note(self, event: str) -> None:
        self.activity.appendleft(event)
""",
        """    def note(self, entry: str) -> None:
        self.activity.appendleft(entry)
""",
        "Rename note's parameter",
        corpus=SIMPLIFY_CORPUS,
        clean=True,
    ),
]
