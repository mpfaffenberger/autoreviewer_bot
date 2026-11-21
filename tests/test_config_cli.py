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
        (["--overflow", "openai:burst", "--overflow-lanes", "4"], {"openai:burst": 4}),
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


SHARED = "http://localhost:8000/v1"
OTHER = "http://localhost:9000/v1"


@pytest.mark.parametrize(
    ("finder", "checker", "flags", "expected"),
    [
        ("openai:a", "openai:b", ["--overflow", "local:c", "--local-url", SHARED], {"local:c": SHARED}),
        (
            "local:a",
            "openai:b",
            ["--local-url", SHARED, "--overflow", "local:c", "--overflow-url", OTHER],
            {"local:a": SHARED, "local:c": OTHER},
        ),
        ("local:a", "openai:b", ["--local-url", SHARED], {"local:a": SHARED}),
        ("openai:a", "local:b", ["--local-url", SHARED], {"local:b": SHARED}),
        ("local:a", "local:b", ["--local-url", SHARED], {"local:a": SHARED, "local:b": SHARED}),
        ("local:a", "local:b", ["--finder-url", OTHER, "--local-url", SHARED], {"local:a": OTHER, "local:b": SHARED}),
        ("local:a", "local:b", ["--local-url", SHARED, "--checker-url", OTHER], {"local:a": SHARED, "local:b": OTHER}),
        ("local:a", "local:b", ["--finder-url", OTHER, "--checker-url", SHARED], {"local:a": OTHER, "local:b": SHARED}),
    ],
)
def test_endpoint_flags(tmp_path: Path, finder: str, checker: str, flags: list[str], expected: dict[str, str]) -> None:
    config = tmp_path / "config.json"
    main(["--output", str(config), "--finder", finder, "--checker", checker, *flags])
    assert PrismConfig.from_file(config).endpoints == expected


@pytest.mark.parametrize(
    ("finder", "checker", "flags", "unused"),
    [
        ("openai:a", "openai:b", ["--local-url", SHARED], "--local-url"),
        ("openai:local:a", "local:b", ["--finder-url", OTHER, "--local-url", SHARED], "--finder-url"),
        ("local:a", "openai:b", ["--checker-url", OTHER, "--local-url", SHARED], "--checker-url"),
        ("local:a", "openai:b", ["--finder-url", OTHER, "--local-url", SHARED], "--local-url"),
    ],
)
def test_unused_url_not_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], finder: str, checker: str, flags: list[str], unused: str
) -> None:
    config = tmp_path / "config.json"
    with pytest.raises(SystemExit, match="2"):
        main(["--output", str(config), "--finder", finder, "--checker", checker, *flags])
    assert f"error: {unused}" in capsys.readouterr().err
    assert not config.exists()


@pytest.mark.parametrize("flags", [[], ["--finder-url", SHARED], ["--checker-url", OTHER]])
def test_missing_endpoint_not_written(tmp_path: Path, flags: list[str]) -> None:
    config = tmp_path / "config.json"
    with pytest.raises(SystemExit, match="2"):
        main(["--output", str(config), "--finder", "local:a", "--checker", "local:b", *flags])
    assert not config.exists()


def test_cli_local_context(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    main(
        [
            "--output",
            str(path),
            "--finder",
            "local:one",
            "--checker",
            "openai:two",
            "--local-url",
            SHARED,
            "--local-context",
            "32768",
        ]
    )
    assert PrismConfig.from_file(path).local_context == {"local:one": 32768}
