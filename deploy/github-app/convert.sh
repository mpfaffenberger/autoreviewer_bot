#!/bin/sh
# Finish GitHub's App manifest flow: trade the one-time code for the App's credentials.
#
#   deploy/github-app/convert.sh CODE
#
# Writes the secrets to $AUTOREVIEWER_CREDENTIAL_DIR (default ~/.config/homelab/) (mode 0600) and prints only non-secret fields.
# The code is single-use and expires one hour after you clicked "Create GitHub App".
set -eu

if [ $# -ne 1 ]; then
    echo "usage: $0 CODE" >&2
    exit 2
fi
CODE=$1
case $CODE in
    *[!A-Za-z0-9]*|'') echo "that doesn't look like a manifest code (expected letters and digits)" >&2; exit 2 ;;
esac

DIR=${AUTOREVIEWER_CREDENTIAL_DIR:-"$HOME/.config/homelab"}
PEM="$DIR/autoreviewer-app.pem"
ENV="$DIR/autoreviewer-app.env"
for existing in "$PEM" "$ENV"; do
    if [ -e "$existing" ]; then
        echo "$existing already exists; refusing to overwrite an App's credentials" >&2
        exit 1
    fi
done

umask 077
mkdir -p "$DIR"
response=$(mktemp)
trap 'rm -f "$response"' EXIT

status=$(curl -sS -o "$response" -w '%{http_code}' -X POST \
    -H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2022-11-28' \
    -- "https://api.github.com/app-manifests/$CODE/conversions")
if [ "$status" != 201 ]; then
    echo "GitHub answered HTTP $status (code expired, already used, or mistyped?):" >&2
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("message", "?"))' "$response" >&2 || true
    exit 1
fi

python3 - "$response" "$PEM" "$ENV" <<'PY'
import json, os, sys

app = json.load(open(sys.argv[1]))
pem_path, env_path = sys.argv[2], sys.argv[3]

def write_private(path: str, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)

write_private(pem_path, app["pem"])
write_private(env_path, "".join(f"{key}={value}\n" for key, value in [
    ("GITHUB_APP_ID", app["id"]),
    ("GITHUB_APP_SLUG", app["slug"]),
    ("GITHUB_APP_CLIENT_ID", app["client_id"]),
    ("GITHUB_APP_CLIENT_SECRET", app["client_secret"]),
    ("GITHUB_WEBHOOK_SECRET", app["webhook_secret"]),
    ("GITHUB_APP_PRIVATE_KEY_PATH", pem_path),
]))

# Non-secret summary only.
print(f"created  {app['name']} (id {app['id']}, slug {app['slug']}) owned by {app['owner']['login']}")
print(f"page     {app['html_url']}")
print(f"install  {app['html_url']}/installations/new")
print(f"perms    {', '.join(f'{k}:{v}' for k, v in sorted(app['permissions'].items()))}")
print(f"events   {', '.join(app['events'])}")
print(f"secrets  {pem_path}, {env_path} (0600)")
PY
