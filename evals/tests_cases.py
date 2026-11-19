"""Seeded test gaps in this repo's own code: tests weakened so they'd miss a break, and new behaviour no test
covers. The code still works and the suite still passes; what's missing is a test that would fail.
Clean controls strengthen a test or refactor well-tested code, plus the correctness suite's clean controls.
"""

from __future__ import annotations

from evals.cases import CLEAN_CONCAT, CLEAN_CONTROLS, LENS_CORPUS, Case

CONFIG, LINT = "src/autoreviewer/web/config.py", "src/autoreviewer/review/lint.py"
TEST_LINT = "tests/test_lint.py"

CASES: list[Case] = [
    # The leftover-import test no longer checks where, how urgent, or which import.
    Case("tests_assert_removed", TEST_LINT,
         '    assert (finding.path, finding.line, finding.priority, finding.kind) == ("util.py", 1, 3, "lint")\n'
         '    assert "`os` imported but unused" in finding.explanation\n',
         '    assert finding.kind == "lint"\n', "Loosen a brittle lint test", corpus=LENS_CORPUS),
    # Passes again if two names on one line collapse into one finding (the bug it guards).
    Case("tests_count_weakened", TEST_LINT, '    assert finding.explanation.count("imported but unused") == 2',
         '    assert finding.explanation.count("imported but unused") >= 1', "Don't depend on ruff's exact messages",
         corpus=LENS_CORPUS),
    Case("tests_skip_added", "tests/test_models.py", "def test_a_bad_pin_stops_startup(",
         '@pytest.mark.skip(reason="noisy on CI")\ndef test_a_bad_pin_stops_startup(', "Quiet a noisy test",
         anchor="@pytest.mark.skip", corpus=LENS_CORPUS),
    # New behaviour (case-insensitive keys in AUTOREVIEWER_MODELS and _LANES) that no test exercises.
    Case("tests_untested_lower", CONFIG, "        pairs.append((key.strip(), val.strip()))",
         "        pairs.append((key.strip().lower(), val.strip()))", "Accept settings keys in any case",
         corpus=LENS_CORPUS),
    Case("tests_untested_init_priority", LINT,
         '        priority=3,\n        confidence=10,\n        title="Remove the unused import",',
         '        priority=2 if path.endswith("__init__.py") else 3,\n        confidence=10,\n'
         '        title="Remove the unused import",', "Rank leftover re-exports higher", corpus=LENS_CORPUS),
    Case("tests_clean_stronger", TEST_LINT, '    assert "`os` imported but unused" in finding.explanation\n',
         '    assert "`os` imported but unused" in finding.explanation\n'
         '    assert finding.title == "Remove the unused import"\n', "Pin the unused-import title", clean=True,
         corpus=LENS_CORPUS),
    CLEAN_CONCAT,
    *CLEAN_CONTROLS,
]  # fmt: skip
