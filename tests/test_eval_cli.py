"""The eval runner's command line and model plan: the same settings the bot reads, from the same environment."""

from __future__ import annotations

import pytest

from evals.run import arguments, plan_from_env
from tests.test_reasoning import effort


def test_the_time_limits_default_to_the_bots_env_included() -> None:
    """From the bot's review of #33: Settings' class defaults ignored AUTOREVIEWER_*_DEADLINE_MINUTES."""
    assert (arguments([], {}).soft_minutes, arguments([], {}).hard_minutes) == (30, 50)
    env = {"AUTOREVIEWER_SOFT_DEADLINE_MINUTES": "20", "AUTOREVIEWER_HARD_DEADLINE_MINUTES": "40"}
    args = arguments([], env)
    assert (args.soft_minutes, args.hard_minutes) == (20, 40)


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["--concurrency", "0"], "--concurrency must be at least 1"),
        (["--soft-minutes", "0"], "--soft-minutes must be greater than 0"),
        (["--soft-minutes", "50", "--hard-minutes", "50"], "below --hard-minutes"),
    ],
)
def test_bad_arguments_stop_the_run(argv: list[str], error: str, capsys: pytest.CaptureFixture[str]) -> None:
    """From the bot's review of #33: the checks were never run."""
    with pytest.raises(SystemExit):
        arguments(argv, {})
    assert error in capsys.readouterr().err


def test_the_plan_reads_the_reasoning_efforts(monkeypatch: pytest.MonkeyPatch) -> None:
    """From the bot's review of #36: plan_from_env carries AUTOREVIEWER_REASONING to the agents."""
    monkeypatch.setenv("BOODLETON_API_KEY", "k")
    monkeypatch.setenv("AUTOREVIEWER_REASONING", "docs=high")
    plan = plan_from_env()
    assert (effort(plan.finder("docs")[0]), effort(plan.finder("tests")[0])) == ("high", None)
