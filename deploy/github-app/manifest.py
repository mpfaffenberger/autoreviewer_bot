"""Generate a local registration form for a new github.com App, or just its manifest."""

import argparse
import html
import json
import re
from urllib.parse import urlsplit


def https_origin(value: str) -> str:
    try:
        url = urlsplit(value)
        valid = (
            url.scheme == "https"
            and url.hostname
            and url.port != 0
            and url.username is None
            and url.path in {"", "/"}
            and not re.search(r"[?#\\\s\x00-\x1f\x7f]", value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("expected a public HTTPS origin, e.g. https://reviews.example.org")
    return f"https://{url.netloc}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, type=https_origin, help="public HTTPS origin of the receiver")
    parser.add_argument("--name", required=True, help="a unique GitHub App name")
    parser.add_argument("--org", help="GitHub organization login (default: your personal account)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a ready-to-submit HTML form")
    args = parser.parse_args()
    if args.org is not None and not re.fullmatch(r"[A-Za-z0-9-]+", args.org):
        parser.error("--org must be an organization login, not a URL")
    owner = f"organizations/{args.org}/" if args.org is not None else ""
    action = f"https://github.com/{owner}settings/apps/new"
    manifest = json.dumps(
        {
            "name": args.name,
            "url": args.base_url,
            "description": "Reviews pull requests for correctness, DRY, and security.",
            "public": False,
            "hook_attributes": {"url": f"{args.base_url}/webhook", "active": True},
            "redirect_url": f"{args.base_url}/github/app-created",
            "default_permissions": {
                "pull_requests": "write",
                "checks": "write",
                "contents": "read",
                "issues": "read",
                "metadata": "read",
            },
            "default_events": [
                "pull_request",
                "pull_request_review",
                "pull_request_review_comment",
                "issue_comment",
                "check_run",
            ],
        },
        indent=2,
    )
    if args.json:
        print(manifest)
        return
    print(f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Register GitHub App</title>
<h1>Register a private GitHub App</h1>
<form method="post" action="{action}">
<label>Review your manifest:<br>
<textarea name="manifest" readonly rows="30" cols="80">{html.escape(manifest)}</textarea></label>
<p><button>Create GitHub App on github.com</button></p></form>
<p>After creation, copy the one-time code from the address bar, even if the page fails to load.
Follow docs/github-app.md to exchange it within one hour.</p></html>''')


if __name__ == "__main__":
    main()
