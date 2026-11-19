"""Run the simplify lens over deliberately bloated functions: how much bloat do its suggestions cut, and
does the code still pass the tests once they're applied?

    set -a; . ~/.config/homelab/autoreviewer.env; set +a
    BOODLETON_BASE_URL=http://127.0.0.1:8484/v1 uv run python -m evals.simplify [--only NAME ...] [--label NAME]

Only the simplifier and its checker run (no correctness review). Results land in
evals/results/simplify-<label>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from autoreviewer.github import Repo
from autoreviewer.review.diff import select
from autoreviewer.review.pipeline import PullRequestText
from autoreviewer.review.progress import Progress
from autoreviewer.review.schemas import Finding
from autoreviewer.review.simplify import SimplifierFactory, simplify
from autoreviewer.review.workspace import read_changes
from autoreviewer.web.app import simplifier_agents
from evals.cases import Case
from evals.run import RESULTS, ROOT, build_repo, plan_from_env
from evals.simplify_cases import CASES
from tests.review_fakes import LocalSource

CONCURRENCY = 2
TEST_TIMEOUT_S = 600


@dataclass
class Result:
    name: str
    clean: bool
    proposed: int
    dropped_by_code: int
    dropped_by_checker: int
    posted: int
    lines_cut: int
    planted: int
    tests_pass: bool | None
    """After applying every posted suggestion; None when nothing was posted."""
    seconds: float
    suggestions: list[str]
    error: str | None = None


async def run_case(case: Case, simplifier: SimplifierFactory, work: Path) -> Result:
    started = time.monotonic()
    planted = 0 if case.clean else len(case.new.splitlines()) - len(case.old.splitlines())
    with tempfile.TemporaryDirectory(dir=work) as scratch:
        try:
            repo, _ = build_repo(case, Path(scratch))
            workspace = await LocalSource(repo).checkout(
                Repo(0, "eval", case.name), head=repo.head, base=repo.base, dest=Path(scratch) / "checkout"
            )
            changes, patches = await read_changes(workspace)
            selection = select(changes)
            pr = PullRequestText(case.title, "", selection.reviewable, selection.skipped, patches)
            run = await simplify(pr, simplifier(workspace), workspace.root, Progress())
            found = run.posted
            tests_pass = _apply_and_test(workspace.root, found) if found else None
        except Exception as error:  # a crashed case is reported, not fatal to the run
            seconds = time.monotonic() - started
            return Result(case.name, case.clean, 0, 0, 0, 0, 0, planted, None, seconds, [], repr(error)[:300])
    dropped_by_code, dropped_by_checker = len(run.dropped_by_code), len(run.dropped_by_checker)
    return Result(
        name=case.name,
        clean=case.clean,
        proposed=len(found) + dropped_by_code + dropped_by_checker,
        dropped_by_code=dropped_by_code,
        dropped_by_checker=dropped_by_checker,
        posted=len(found),
        lines_cut=sum(f.lines_cut for f in found),
        planted=planted,
        tests_pass=tests_pass,
        seconds=time.monotonic() - started,
        suggestions=[f"{f.path}:{f.start_line}-{f.line} {f.title} ({f.lines_cut} cut)" for f in found],
    )


def _apply_and_test(root: Path, found: list[Finding]) -> bool:
    """Apply every suggestion (bottom-up, so line numbers stay valid) and run the checkout's own tests."""
    for finding in sorted(found, key=lambda f: (f.path, -(f.start_line or f.line))):
        path = root / finding.path
        lines = path.read_text().splitlines()
        start = finding.start_line or finding.line
        lines[start - 1 : finding.line] = (finding.replacement or "").splitlines()
        path.write_text("\n".join(lines) + "\n")
    # The checkout's src shadows this repo's editable install, so the tests exercise the patched code.
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    command = [
        "uv",
        "run",
        "--project",
        str(ROOT),
        "--frozen",
        "python",
        "-m",
        "pytest",
        "-q",
        "-x",
        "-p",
        "no:cacheprovider",
    ]
    tests = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=TEST_TIMEOUT_S)
    return tests.returncode == 0


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="*", help="case names to run (default: all)")
    parser.add_argument("--label", default=time.strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args()

    simplifier = simplifier_agents(plan_from_env())

    cases = [c for c in CASES if not args.only or c.name in args.only]
    slots = asyncio.Semaphore(CONCURRENCY)
    with tempfile.TemporaryDirectory(prefix="autoreviewer-simplify-eval-") as work:

        async def bounded(case: Case) -> Result:
            async with slots:
                result = await run_case(case, simplifier, Path(work))
                print(
                    f"  {case.name:22} {'clean' if case.clean else 'bloat'} posted={result.posted} "
                    f"cut={result.lines_cut}/{result.planted} tests_pass={result.tests_pass} "
                    f"dropped(code={result.dropped_by_code}, checker={result.dropped_by_checker}) "
                    f"{result.seconds:4.0f}s {result.error or ''}",
                    flush=True,
                )
                return result

        results = await asyncio.gather(*(bounded(case) for case in cases))

    bloat = [r for r in results if not r.clean]
    summary = {
        "label": args.label,
        "recovered": f"{sum(r.lines_cut for r in bloat)}/{sum(r.planted for r in bloat)} planted lines",
        "recovery": sum(r.lines_cut for r in bloat) / max(1, sum(r.planted for r in bloat)),
        "cases_with_broken_tests": [r.name for r in results if r.tests_pass is False],
        "suggestions_on_clean_cases": sum(r.posted for r in results if r.clean),
        "errors": sum(1 for r in results if r.error),
        "minutes": round(sum(r.seconds for r in results) / 60, 1),
    }
    print(json.dumps(summary, indent=2))
    RESULTS.mkdir(exist_ok=True)
    out = {"summary": summary, "cases": [asdict(r) for r in results]}
    (RESULTS / f"simplify-{args.label}.json").write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
