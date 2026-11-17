"""Run the reviewer over the seeded-bug corpus and report recall and false alarms.

    set -a; . ~/.config/homelab/autoreviewer.env; set +a
    BOODLETON_BASE_URL=http://127.0.0.1:8484/v1 uv run python -m evals.run [--only NAME ...] [--label NAME]

Uses the real models through Boodleton and the reviewer code in this working tree; nothing touches
GitHub. Results land in evals/results/<label>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from autoreviewer.github import PullRequestState, Repo
from autoreviewer.review.agents import (
    DEEPSEEK_CONTEXT_WINDOW,
    QWEN_CONTEXT_WINDOW,
    boodleton_model,
    correctness_agent,
    verifier_agent,
)
from autoreviewer.review.job import CheckResult, Reviewed, review_change
from autoreviewer.review.schemas import Finding
from evals.cases import CASES, HIT_TOLERANCE, Case
from tests.review_fakes import FakeGitHub, GitRepo, LocalSource, git

ROOT = Path(__file__).resolve().parents[1]
RESULTS = Path(__file__).with_name("results")
CONCURRENCY = 2


def _snapshot(path: Path, commit: str) -> None:
    """Replace the working tree with `commit`'s files (the eval repo gets its own fresh history)."""
    for child in path.iterdir():
        if child.name != ".git":
            subprocess.run(["rm", "-rf", str(child)], check=True)
    archive = subprocess.run(["git", "archive", commit], cwd=ROOT, check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x"], cwd=path, input=archive, check=True)


def build_repo(case: Case, parent: Path) -> tuple[GitRepo, list[int]]:
    """`base` and the mutated `head` as two commits; returns the seeded line numbers."""
    path = parent / case.name
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    base_commit, head_commit = case.commits
    _snapshot(path, base_commit)
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    base = git(path, "rev-parse", "HEAD")
    if head_commit != base_commit:
        _snapshot(path, head_commit)

    target = path / case.path
    mutated = target.read_text()
    if case.old:
        count = mutated.count(case.old)
        if count == 0 or (count > 1 and not case.replace_all):
            raise ValueError(f"{case.name}: expected one match of {case.old!r} in {case.path}, found {count}")
        mutated = mutated.replace(case.old, case.new)
        target.write_text(mutated)
    git(path, "add", "-A")
    git(path, "commit", "-qm", case.title)
    head = git(path, "rev-parse", "HEAD")
    lines = [] if case.clean else [n for n, line in enumerate(mutated.splitlines(), 1) if case.target in line]
    if not case.clean and not lines:
        raise ValueError(f"{case.name}: anchor {case.target!r} not found after mutation")
    return GitRepo(path, base, head), lines


def hits(findings: list[Finding], case: Case, lines: list[int]) -> bool:
    return any(f.path == case.path and any(abs(f.line - n) <= HIT_TOLERANCE for n in lines) for f in findings)


@dataclass
class Result:
    name: str
    clean: bool
    lens_hit: bool
    posted_hit: bool
    raw: int
    posted: int
    false_alarms: int
    seconds: float
    error: str | None = None
    blocking_hit: bool = False
    """The seeded bug was posted at a priority that blocks the merge."""
    findings: list[str] = field(default_factory=list[str])
    """Every raw finding and its fate, for triaging "false alarms" that may be real bugs in the host commit."""


async def run_case(case: Case, agents: object, work: Path) -> Result:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(dir=work) as scratch:
        repo, lines = build_repo(case, Path(scratch))
        state = PullRequestState(
            head_sha=repo.head, base_sha=repo.base, state="open", draft=False, title=case.title, body=""
        )
        try:
            result = await review_change(
                reader=FakeGitHub(repo), source=LocalSource(repo), agents=agents, work_dir=Path(scratch),  # type: ignore[arg-type]
                repo=Repo(0, "eval", case.name), number=1, state=state, head_sha=repo.head,
            )  # fmt: skip
        except Exception as error:  # a crashed case is reported, not fatal to the run
            return Result(case.name, case.clean, False, False, 0, 0, 0, time.monotonic() - started, repr(error)[:300])
    seconds = time.monotonic() - started
    if isinstance(result, CheckResult):
        return Result(case.name, case.clean, False, False, 0, 0, 0, seconds, f"check: {result.title}")
    assert isinstance(result, Reviewed)
    outcome = result.outcome
    posted = [*outcome.inline, *outcome.general]
    low_confidence = getattr(outcome, "low_confidence", [])  # the baseline has none
    raw = [*posted, *(f for f, _ in outcome.rejected), *(f for f, _ in outcome.unverified), *low_confidence]
    posted_hit = hits(posted, case, lines)
    blocks = getattr(outcome, "is_blocking", lambda finding: True)  # the baseline blocks on every finding
    false_alarms = len(posted) if case.clean else len(posted) - (1 if posted_hit else 0)
    dropped = [*outcome.rejected, *outcome.unverified, *((f, "low confidence") for f in low_confidence)]
    fates = [(f, "posted" if blocks(f) else "advisory") for f in posted] + [
        (f, f"dropped: {why}") for f, why in dropped
    ]
    return Result(
        case.name,
        case.clean,
        hits(raw, case, lines),
        posted_hit,
        len(raw),
        len(posted),
        false_alarms,
        seconds,
        blocking_hit=hits([f for f in posted if blocks(f)], case, lines),
        findings=[f"{f.path}:{f.line} {_grade(f)} {f.title} [{fate}]" for f, fate in fates],
    )


def _grade(finding: Finding) -> str:
    """Priority and confidence when the schema has them (the baseline's doesn't)."""
    priority, confidence = getattr(finding, "priority", None), getattr(finding, "confidence", None)
    return f"P{priority} c{confidence}" if priority is not None else str(getattr(finding, "severity", ""))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="*", help="case names to run (default: all)")
    parser.add_argument("--label", default=time.strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args()

    base_url = os.environ.get("BOODLETON_BASE_URL", "http://127.0.0.1:8484/v1")
    key = os.environ["BOODLETON_API_KEY"]
    lens_model = boodleton_model(
        os.environ.get("AUTOREVIEWER_LENS_MODEL", "qwen3.8-flash-next"), base_url=base_url, api_key=key
    )
    verifier_model = boodleton_model(
        os.environ.get("AUTOREVIEWER_VERIFIER_MODEL", "deepseek-v4.1-flash"), base_url=base_url, api_key=key
    )

    def agents(workspace: object) -> object:
        return (
            correctness_agent(lens_model, workspace, context_window=QWEN_CONTEXT_WINDOW),  # type: ignore[arg-type]
            verifier_agent(verifier_model, workspace, context_window=DEEPSEEK_CONTEXT_WINDOW),  # type: ignore[arg-type]
        )

    cases = [c for c in CASES if not args.only or c.name in args.only]
    slots = asyncio.Semaphore(CONCURRENCY)
    with tempfile.TemporaryDirectory(prefix="autoreviewer-eval-") as work:

        async def bounded(case: Case) -> Result:
            async with slots:
                result = await run_case(case, agents, Path(work))
                kind = "clean" if case.clean else "bug  "
                print(
                    f"  {case.name:26} {kind} lens_hit={result.lens_hit!s:5} posted_hit={result.posted_hit!s:5} "
                    f"raw={result.raw} posted={result.posted} false_alarms={result.false_alarms} "
                    f"{result.seconds:5.0f}s {result.error or ''}",
                    flush=True,
                )
                return result

        results = await asyncio.gather(*(bounded(case) for case in cases))

    bugs = [r for r in results if not r.clean]
    clean = [r for r in results if r.clean]
    summary = {
        "label": args.label,
        "bugs": len(bugs),
        "lens_recall": sum(r.lens_hit for r in bugs) / max(1, len(bugs)),
        "posted_recall": sum(r.posted_hit for r in bugs) / max(1, len(bugs)),
        "blocking_recall": sum(r.blocking_hit for r in bugs) / max(1, len(bugs)),
        "false_alarms_on_bug_cases": sum(r.false_alarms for r in bugs),
        "clean_cases_with_findings": sum(1 for r in clean if r.posted),
        "errors": sum(1 for r in results if r.error),
        "minutes": round(sum(r.seconds for r in results) / 60, 1),
    }
    print(json.dumps(summary, indent=2))
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{args.label}.json").write_text(
        json.dumps({"summary": summary, "cases": [r.__dict__ for r in results]}, indent=2)
    )


if __name__ == "__main__":
    asyncio.run(main())
