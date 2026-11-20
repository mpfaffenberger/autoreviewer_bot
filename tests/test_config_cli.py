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


def test_invalid_config_not_created(tmp_path: Path) -> None:
    config = tmp_path / "bad.json"
    with pytest.raises(SystemExit, match="2"):
        main(["--output", str(config), "--finder", "invalid", "--checker", "openai:other"])
    assert not config.exists()
