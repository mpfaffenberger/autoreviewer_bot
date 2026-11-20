"""Exercise the real local chat adapter without sending a model request."""

import pytest
from pydantic import ValidationError
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.models.openai import OpenAIChatModel

from autoreviewer.model_config import PrismConfig

FINDER = "local:org/finder:q4"
CHECKER = "local:checker"
ENDPOINTS = {FINDER: "http://[::1]:9000/v1", CHECKER: "https://checker.example/api/openai/v2/"}


@pytest.mark.parametrize("key", [None, "  ", "deployment-key"])
def test_local_chat_adapter(key: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "cloud-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://cloud.example/v1")
    if key is None:
        monkeypatch.delenv("PRISM_LOCAL_API_KEY", raising=False)
    else:
        monkeypatch.setenv("PRISM_LOCAL_API_KEY", key)
    config = PrismConfig(finder=FINDER, checker=CHECKER, endpoints=ENDPOINTS)
    for spec, url in ENDPOINTS.items():
        model, _ = config.resolve(spec)
        assert isinstance(model, ConcurrencyLimitedModel)
        chat = model.wrapped
        assert isinstance(chat, OpenAIChatModel)
        assert chat.model_name == spec.removeprefix("local:")
        assert str(chat.client.base_url) == url.rstrip("/") + "/"
        assert chat.client.api_key == ((key or "").strip() or "prism-local")
        assert "max_tokens" not in (chat.settings or {})


@pytest.mark.parametrize("extra", ["openai:checker", "local:unknown", "org/finder:q4"])
def test_only_configured_local_keys_allowed(extra: str) -> None:
    with pytest.raises(ValidationError, match="endpoints"):
        PrismConfig(
            finder=FINDER, checker="openai:checker", endpoints=dict.fromkeys([FINDER, extra], ENDPOINTS[FINDER])
        )


def test_each_local_model_requires_endpoint() -> None:
    with pytest.raises(ValidationError, match=r"missing endpoints.*local:checker"):
        PrismConfig(finder=FINDER, checker=CHECKER, endpoints={FINDER: ENDPOINTS[FINDER]})


@pytest.mark.parametrize(
    "url",
    [
        "http://host/",
        "localhost:8000/v1",
        "ftp://host/v1",
        "http:///v1",
        "https://host:bad/v1",
        "http://bad host/v1",
        "https://user@host/v1",
        "https://user:pass@host/v1",
        "https://host/v1?key=value",
        "http://host/v1#part",
        "http://host/v1?",
        "http://host/v1#",
        "http://host/\\other",
        "https://host/\nv1",
    ],
)
def test_invalid_local_endpoint(url: str) -> None:
    with pytest.raises(ValidationError, match="endpoints"):
        PrismConfig(finder=FINDER, checker="openai:checker", endpoints={FINDER: url})
