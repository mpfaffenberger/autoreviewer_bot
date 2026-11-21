import pytest
from pydantic import ValidationError

from autoreviewer.model_config import PrismConfig


@pytest.mark.parametrize(
    "overrides, roles",
    [
        ({}, ("openai:z", "openai:a")),
        ({"checker": "openai:z"}, ("openai:a", "openai:z")),
        ({"finder": "openai:a"}, ("openai:a", "openai:z")),
    ],
)
def test_pool_preferences(overrides: dict[str, str], roles: tuple[str, str]) -> None:
    pool = {"openai:z": 1, "openai:a": 3, "local:c": 2}
    config = PrismConfig.model_validate(
        {
            "pool": pool,
            **overrides,
            "endpoints": {"local:c": "http://localhost/v1"},
            "local_context": {"local:c": 1024},
            "compact_at": {"local:c": 128},
            "lanes": {"openai:z": 1},
        }
    )
    assert (config.finder, config.checker) == roles
    assert list((config.pool or {}).items()) == list(pool.items())
    assert [config.lane_count(spec) for spec in pool] == [1, 3, 2]
    assert config.resolve("local:c")[1] == 128


@pytest.mark.parametrize(
    "pool",
    [
        {},
        {"openai:a": 1},
        {"openai:a": 1, "openai:b": 0},
        {"openai:a": False, "openai:b": 1},
        {"openai:a": "1", "openai:b": 1},
        {"openai:a": 1, "openai:b": 2, "invalid": 3},
    ],
)
def test_invalid_pool(pool: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PrismConfig.model_validate({"finder": "openai:a", "checker": "openai:b", "pool": pool})


def test_conflicting_lanes() -> None:
    with pytest.raises(ValidationError, match="lanes"):
        PrismConfig.model_validate({"pool": {"openai:a": 2, "openai:b": 1}, "lanes": {"openai:a": 3}})
