from pathlib import Path

import pytest

from autoreviewer.config_cli import main
from autoreviewer.model_config import PrismConfig


def test_pool_only_cli(tmp_path: Path) -> None:
    path = tmp_path / "prism.json"
    main(
        [
            "--output",
            str(path),
            "--pool-model",
            "local:z=3",
            "--pool-model",
            "openai:b=1",
            "--pool-model",
            "local:a=2",
            "--finder-url",
            "http://finder/v1",
            "--local-url",
            "http://shared/v1",
            "--local-context",
            "8192",
        ]
    )
    config = PrismConfig.from_file(path)
    assert list((config.pool or {}).items()) == [("local:z", 3), ("openai:b", 1), ("local:a", 2)]
    assert (config.finder, config.checker) == ("local:z", "openai:b")
    assert config.endpoints == {"local:z": "http://finder/v1", "local:a": "http://shared/v1"}
    assert config.local_context == dict.fromkeys(config.endpoints, 8192)


@pytest.mark.parametrize("extra", [[], ["openai:a=2"], ["openai:b=0"], ["openai:b"], ["openai:b=1.5"]])
def test_invalid_pool_not_written(tmp_path: Path, extra: list[str]) -> None:
    path = tmp_path / "prism.json"
    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "--output",
                str(path),
                "--pool-model",
                "openai:a=1",
                *[arg for spec in extra for arg in ("--pool-model", spec)],
            ]
        )
    assert not path.exists()
