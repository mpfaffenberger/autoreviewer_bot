"""Seeded docs and contract drift in this repo's own code: a change leaves a docstring, design doc, error message,
or documented setting saying something the code no longer does. Each change works; the text is what's wrong.
Two more cases add slop (pstack's unslop rules) in text the PR writes.
Clean controls change code without making any text untrue, plus the correctness suite's clean controls.
"""

from __future__ import annotations

from evals.cases import CLEAN_CONCAT, CLEAN_CONTROLS, LENS_CORPUS, Case

APP, CONFIG, SERVERS = "src/autoreviewer/web/app.py", "src/autoreviewer/web/config.py", "src/autoreviewer/servers.py"

CASES: list[Case] = [
    # Its docstring (right below) and DESIGN.md both say 5.
    Case("docs_dry_cap", APP, "MAX_DRY_POSTED = 5\n", "MAX_DRY_POSTED = 8\n", "Let DRY post a few more findings",
         corpus=LENS_CORPUS),
    # DESIGN.md §7: "default Qwen 4 and DeepSeek 4".
    Case("docs_lanes_default", SERVERS, "DEFAULT_MODEL_LANES = {QWEN: 4, DEEPSEEK: 4}",
         "DEFAULT_MODEL_LANES = {QWEN: 4, DEEPSEEK: 2}", "Leave DeepSeek more room for other users",
         corpus=LENS_CORPUS),
    # DESIGN.md: "`PRISM_DRY=false` turns it off": a contract the env file relies on.
    Case("docs_env_rename", CONFIG, 'dry=parse_bool(env, "PRISM_DRY", default=True)',
         'dry=parse_bool(env, "PRISM_DRY_LENS", default=True)', "Name the DRY switch after the lens",
         corpus=LENS_CORPUS),
    # The error message on the next line still says "lanes >= 1".
    Case("docs_error_message", CONFIG, "        if not count.isdigit() or int(count) < 1:",
         "        if not count.isdigit() or int(count) < 0:", "Allow pausing a model with zero lanes",
         corpus=LENS_CORPUS),
    # Its docstring says "4 streams of 224k tokens"; DESIGN.md §7 too.
    Case("docs_window", SERVERS, "QWEN_CONTEXT_WINDOW = 229_376", "QWEN_CONTEXT_WINDOW = 196_608",
         "Match Qwen's window to the server", corpus=LENS_CORPUS),
    # unslop 7, 8, 3: "serves as", "crucial", "seamlessly", a trailing "-ing" phrase.
    Case("docs_slop_docstring", APP, "def _and(names: list[str]) -> str:\n",
         'def _and(names: list[str]) -> str:\n    """This helper serves as a crucial utility, seamlessly joining names '
         'and ensuring a vibrant, readable footer."""\n', "Document the footer helper", anchor="serves as a crucial",
         corpus=LENS_CORPUS),
    # unslop 7, 13, 26, 31: "leveraging", "fosters", "pivotal", an em dash, "landscape".
    Case("docs_slop_comment", SERVERS, "DEFAULT_MODEL_LANES = {QWEN: 4, DEEPSEEK: 4}",
         "# Leveraging these lanes fosters robust throughput \u2014 a pivotal part of the serving landscape.\n"
         "DEFAULT_MODEL_LANES = {QWEN: 4, DEEPSEEK: 4}", "Explain the lane defaults", anchor="Leveraging these lanes",
         corpus=LENS_CORPUS),
    Case("docs_clean_helper_name", APP, "_and(", "_join_names(", "Name the footer helper", replace_all=True,
         clean=True, corpus=LENS_CORPUS),
    CLEAN_CONCAT,
    *CLEAN_CONTROLS,
]  # fmt: skip
