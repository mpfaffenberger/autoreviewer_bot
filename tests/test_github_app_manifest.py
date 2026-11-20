import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SETUP = Path(__file__).resolve().parents[1] / "deploy/github-app"
ORIGIN = "https://reviews.example.org:8443"
NAME = "Reviewer & </textarea>"


def generate(origin: str, *options: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SETUP / "manifest.py"), "--base-url", origin, "--name", NAME, *options],
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("org", [None, "example-org"])
def test_registration_form(org: str | None) -> None:
    result = generate(ORIGIN + "/", *(["--org", org] if org else []))
    result.check_returncode()
    owner_path = f"organizations/{org}/" if org else ""
    assert f'method="post" action="https://github.com/{owner_path}settings/apps/new"' in result.stdout
    field = re.search(r'<textarea name="manifest"[^>]*>(.*?)</textarea>', result.stdout, re.S)
    assert field
    manifest = json.loads(html.unescape(field[1]))
    assert manifest == json.loads(generate(ORIGIN, "--json").stdout)
    assert manifest["name"] == NAME
    assert manifest["public"] is False
    assert manifest["url"] == ORIGIN
    assert manifest["hook_attributes"] == {"url": ORIGIN + "/webhook", "active": True}
    assert manifest["redirect_url"] == ORIGIN + "/github/app-created"
    assert manifest["default_permissions"] == {
        "pull_requests": "write",
        "checks": "write",
        "contents": "read",
        "issues": "read",
        "metadata": "read",
    }
    assert set(manifest["default_events"]) == {
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "issue_comment",
        "check_run",
    }


@pytest.mark.parametrize("origin", ["http://host", "https://user@host", ORIGIN + "/webhook", ORIGIN + "?x", "https://"])
def test_invalid_origin(origin: str) -> None:
    assert generate(origin).returncode == 2


@pytest.mark.parametrize("custom", [True, False])
def test_convert_preserves_existing_credentials(tmp_path: Path, custom: bool) -> None:
    directory = tmp_path / ("other path" if custom else ".config/homelab")
    directory.mkdir(parents=True)
    pem = directory / "autoreviewer-app.pem"
    pem.write_text("original")
    env = {"HOME": str(tmp_path), "PATH": os.defpath}
    if custom:
        env["AUTOREVIEWER_CREDENTIAL_DIR"] = str(directory)
    result = subprocess.run([SETUP / "convert.sh", "FakeCode"], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert str(pem) in result.stderr
    assert pem.read_text() == "original"
