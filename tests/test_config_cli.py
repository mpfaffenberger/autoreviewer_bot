from pathlib import Path

import pytest

from autoreviewer.config_cli import main
from autoreviewer.model_config import PrismConfig


def test_cli_roundtrip_and_no_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.json"
    answers = iter(["openai:first", "github-copilot:other"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    main(["--output", str(config)])
    expected = PrismConfig(finder="openai:first", checker="github-copilot:other")
    assert PrismConfig.from_file(config) == expected
    with pytest.raises(SystemExit, match="2"):
        main(["--output", str(config), "--finder", "openai:new", "--checker", "openai:other"])
    assert PrismConfig.from_file(config) == expected


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (["--finder-lanes", "3"], {"openai:first": 3}),
        (["--checker-lanes", "2"], {"openai:other": 2}),
        (["--finder-lanes", "3", "--checker-lanes", "2"], {"openai:first": 3, "openai:other": 2}),
    ],
)
def test_lane_flags(tmp_path: Path, flags: list[str], expected: dict[str, int]) -> None:
    config = tmp_path / "config.json"
    main(["--output", str(config), "--finder", "openai:first", "--checker", "openai:other", *flags])
    assert PrismConfig.from_file(config).lanes == expected


@pytest.mark.parametrize(
    ("flag", "value"), [("--finder-lanes", "0"), ("--checker-lanes", "-2"), ("--finder-lanes", "1.5")]
)
def test_invalid_lanes_not_written(tmp_path: Path, flag: str, value: str) -> None:
    config = tmp_path / "config.json"
    with pytest.raises(SystemExit, match="2"):
        main(["--output", str(config), "--finder", "openai:first", "--checker", "openai:other", flag, value])
    assert not config.exists()


def test_invalid_config_not_created(tmp_path: Path) -> None:
    config = tmp_path / "bad.json"
    with pytest.raises(SystemExit, match="2"):
        main(["--output", str(config), "--finder", "invalid", "--checker", "openai:other"])
    assert not config.exists()
