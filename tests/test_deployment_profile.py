"""The deployment profile is the one place the gateway plan's models, tuning and endpoints are named."""

import json
import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from autoreviewer.deployment_profile import DeploymentProfile, load_deployment_profile

PACKAGED = files("autoreviewer").joinpath("deployment_profile.json")


def packaged() -> dict[str, Any]:
    return json.loads(PACKAGED.read_bytes())


def test_replacement_profile(tmp_path: Path) -> None:
    """Models, tuning, role assignment, gateway URL and overflow model all follow the file, with no code change."""
    profile = packaged()
    profile.update(
        base_url="https://gateway.example/v1",
        reply_tokens=10,
        reasoning={"tests.verifier": "low"},
        overflow_model="other:remote",
    )
    # Slot names are the operator's own, and there may be more than two models.
    profile["models"] = {
        "fast": dict(id="first", context_window=300, compact_at=250, lanes=1),
        "careful": dict(id="second", context_window=400, compact_at=320, lanes=8),
        "spare": dict(id="third", context_window=500, compact_at=100, lanes=2),
    }
    profile["roles"] = {role: "careful" if role.endswith(".verifier") else "fast" for role in profile["roles"]}
    profile["roles"].update({"docs": "careful", "docs.verifier": "spare", "conversation": "spare"})
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(profile))
    # A fresh interpreter is essential: agents and evals read the profile at import.
    script = """
import inspect
from autoreviewer import servers as s
from autoreviewer.review import agents
from autoreviewer.web.config import Settings, models_from
settings = Settings.from_env()
assert s.CONTEXT_WINDOWS == {'first': 300, 'second': 400, 'third': 500}
assert s.COMPACT_AT == {'first': 250, 'second': 320, 'third': 100}
assert settings.model_lanes == {'first': 1, 'second': 8, 'third': 2}
assert s.REPLY_TOKENS == 10
assert settings.boodleton_base_url == 'https://gateway.example/v1'
assert settings.overflow_model == 'other:remote'
assert settings.reasoning == {'tests.verifier': 'low'}
assert settings.models['correctness'] == 'first' and settings.models['docs'] == 'second'
assert settings.models['docs.verifier'] == settings.models['conversation'] == 'third'
assert models_from({'PRISM_MODELS': 'conversation=second'})['conversation'] == 'second'
# An agent built without a plan compacts where its role's model does.
default = lambda agent: inspect.signature(agent).parameters['compact_at'].default
assert (default(agents.correctness_agent), default(agents.conversation_agent)) == (250, 100)
"""
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PRISM_", "BOODLETON_"))}
    env.update(
        PRISM_DEPLOYMENT_PROFILE=str(path),
        PRISM_PRIVATE_KEY_PATH=str(path),  # any readable file: the key is not used
        GITHUB_APP_ID="1",
        GITHUB_APP_SLUG="app",
        GITHUB_WEBHOOK_SECRET="secret",
        BOODLETON_API_KEY="fake",
    )
    subprocess.run([sys.executable, "-c", script], env=env, check=True)


def test_invalid_override_does_not_fall_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "invalid.json"
    monkeypatch.setenv("PRISM_DEPLOYMENT_PROFILE", str(path))
    with pytest.raises(ValueError, match=r"invalid.json"):
        load_deployment_profile()
    profile = packaged()
    profile["roles"]["conversation"] = "typo"
    path.write_text(json.dumps(profile))
    with pytest.raises(ValueError, match="unknown model slots"):
        load_deployment_profile()


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"overflow_model": "local:burst"}, "local: spec"),
        ({"overflow_model": "no-provider"}, "provider:model"),
        ({"base_url": "gateway.example/v1"}, "base_url"),
    ],
)
def test_profile_rejects(change: dict[str, str], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        DeploymentProfile.model_validate_json(json.dumps({**packaged(), **change}))


def test_overflow_is_optional() -> None:
    profile = packaged()
    del profile["overflow_model"], profile["reasoning"]
    loaded = DeploymentProfile.model_validate_json(json.dumps(profile))
    assert loaded.overflow_model is None and loaded.reasoning == {}


def test_python_source_names_no_model() -> None:
    """Model IDs, the gateway URL and the overflow model live in the profile alone, so swapping one never
    means editing code. This fails if a concrete name from the packaged profile reappears in a module."""
    profile = packaged()
    overflow = profile["overflow_model"]
    literals = {model["id"] for model in profile["models"].values()}
    literals |= {profile["base_url"], overflow, overflow.split(":", 1)[1]}
    root = Path(str(files("autoreviewer")))
    found = [
        f"{path.relative_to(root)}: {literal}"
        for path in sorted(root.rglob("*.py"))
        for literal in sorted(literals)
        if literal in path.read_text()
    ]
    assert not found
