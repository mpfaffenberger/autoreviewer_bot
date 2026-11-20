# Install your own Prism GitHub App

This creates a **new private App on github.com**. If you're using the existing hosted App, skip registration: nothing here needs to be run to keep it working. Your receiver's hostname, your GitHub App's slug, and the `autoreviewer` check name are three different things.

### 1. Register the App

Choose a public HTTPS hostname you control and a unique App name. From the repo root, generate a local file (Python 3, no packages or credentials needed):

```sh
python3 deploy/github-app/manifest.py --base-url https://reviews.example.org \
  --name 'Acme PR Reviewer' > register-app.html
```

Replace both example values. Use the origin only, not `/webhook` or a path prefix. For an organization-owned App add `--org YOUR-ORG` before the redirection. Open `register-app.html` locally in a browser signed into the intended GitHub account; no web server is needed. Inspect the manifest, submit, and confirm on GitHub. The form requests write access to pull requests and checks, and read access to contents, issues, and metadata. To inspect the same settings as plain JSON, use `--json` instead of saving HTML.

### 2. Exchange the code on the receiver host

GitHub redirects your browser to `https://YOUR-HOST/github/app-created?code=…`. **A 404 is expected:** there's no callback handler, and the receiver needn't be running yet. Copy only the `code` value. It is secret, single-use, and expires after one hour; keep the query string out of proxy logs. From the checkout in Bash:

```bash
export AUTOREVIEWER_CREDENTIAL_DIR="$HOME/.config/autoreviewer"
read -rsp 'Manifest code: ' code; echo
deploy/github-app/convert.sh "$code"
unset code
```

The script requires `curl` and Python 3, prints the App slug and installation URL, and writes `autoreviewer-app.env` and `autoreviewer-app.pem` with mode 0600. Keep both private and outside the checkout. It refuses to overwrite either file. Omitting `AUTOREVIEWER_CREDENTIAL_DIR` retains the existing `~/.config/homelab` layout.

### 3. Start the receiver and route HTTPS

For a rootless Podman deployment, build from the repo root. Run `uv run prism-config --output "$AUTOREVIEWER_CREDENTIAL_DIR/prism.json"` and select two distinct tool-capable models. Create a mode-0600 `model.env` beside it with the [provider credentials](providers.md) your models need (one `KEY=value` per line), plus `AUTOREVIEWER_CONVERSATIONS=true` for thread replies. These paths and credentials must be accessible to the receiver, not to the repositories it reviews.

```sh
podman build -t localhost/autoreviewer:latest -f deploy/web/Containerfile .
mkdir -p "$HOME/.local/share/autoreviewer"
podman run -d --name autoreviewer --userns=keep-id:uid=999,gid=999 \
  -p 127.0.0.1:8000:8000 --env-file "$AUTOREVIEWER_CREDENTIAL_DIR/autoreviewer-app.env" \
  -e PRISM_CONFIG=/run/secrets/prism.json \
  -v "$AUTOREVIEWER_CREDENTIAL_DIR/prism.json:/run/secrets/prism.json:ro,Z" \
  --env-file "$AUTOREVIEWER_CREDENTIAL_DIR/model.env" \
  -v "$AUTOREVIEWER_CREDENTIAL_DIR/autoreviewer-app.pem:/run/secrets/github-app.pem:ro,Z" \
  -v "$HOME/.local/share/autoreviewer:/var/lib/autoreviewer:Z" localhost/autoreviewer:latest
```

Point DNS at your ingress and configure a TLS reverse proxy. For example, with Caddy **on the same host**:

```caddyfile
reviews.example.org {
    reverse_proxy 127.0.0.1:8000
}
```

`curl -fsS https://reviews.example.org/healthz` should succeed. Allow GitHub to POST `/webhook` without a login or browser challenge; preserve the body and signature headers. The receiver verifies signatures with `GITHUB_WEBHOOK_SECRET` from the generated env file. It reads `GITHUB_APP_ID` and `GITHUB_APP_SLUG` from that file too. For a different key mount, set `AUTOREVIEWER_PRIVATE_KEY_PATH` to the **container-side** path.

For a persistent systemd service, adapt [the example Quadlet](../deploy/web/autoreviewer.container) rather than installing it verbatim: change its host paths, env files and network to match your proxy. That example uses a shared container network rather than publishing port 8000.

### 4. Install and verify

Open the **install** URL printed by `convert.sh` and select just the repositories to review. Open a non-draft PR there, or as its author comment `@YOUR-APP-SLUG review` on an existing open PR. Use the actual slug GitHub returned, not the display name or the hosted bot's handle. Look for the `autoreviewer` check on the head commit; that's also the name to require in branch protection. If nothing appears, inspect the App's **Advanced → Recent deliveries** first: verify the delivery URL and response status before troubleshooting review or model logs.
