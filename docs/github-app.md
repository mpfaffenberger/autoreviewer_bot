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
export PRISM_CREDENTIAL_DIR="$HOME/.config/prism"
read -rsp 'Manifest code: ' code; echo
deploy/github-app/convert.sh "$code"
unset code
```

The script requires `curl` and Python 3, prints the App slug and installation URL, and writes `prism-app.env` and `prism-app.pem` with mode 0600. Keep both private and outside the checkout. It refuses to overwrite either file. Omitting `PRISM_CREDENTIAL_DIR` uses `~/.config/prism`.

### 3. Start the receiver and route HTTPS

For a rootless Podman deployment, build from the repo root. Run `uv run prism-config --output "$PRISM_CREDENTIAL_DIR/prism.json"` and select two distinct tool-capable models. Create a mode-0600 `prism.env` beside it with the [provider credentials](providers.md) your models need (one `KEY=value` per line), plus `PRISM_CONVERSATIONS=true` for thread replies. These paths and credentials must be accessible to the receiver, not to the repositories it reviews.

```sh
podman build -t localhost/prism:latest -f deploy/web/Containerfile .
mkdir -p "$HOME/.local/share/prism"
podman run -d --name prism --userns=keep-id:uid=999,gid=999 \
  -p 127.0.0.1:8000:8000 --env-file "$PRISM_CREDENTIAL_DIR/prism-app.env" \
  -e PRISM_PRIVATE_KEY_PATH=/run/prism/github-app.pem \
  -e PRISM_CONFIG=/run/prism/prism.json \
  -v "$PRISM_CREDENTIAL_DIR/prism.json:/run/prism/prism.json:ro,Z" \
  --env-file "$PRISM_CREDENTIAL_DIR/prism.env" \
  -v "$PRISM_CREDENTIAL_DIR/prism-app.pem:/run/prism/github-app.pem:ro,Z" \
  -v "$HOME/.local/share/prism:/var/lib/prism:Z" localhost/prism:latest
```

Point DNS at your ingress and configure a TLS reverse proxy. For example, with Caddy **on the same host**:

```caddyfile
reviews.example.org {
    reverse_proxy 127.0.0.1:8000
}
```

`curl -fsS https://reviews.example.org/healthz` should succeed. Allow GitHub to POST `/webhook` without a login or browser challenge; preserve the body and signature headers. The receiver verifies signatures with `GITHUB_WEBHOOK_SECRET` from the generated env file. It reads `GITHUB_APP_ID` and `GITHUB_APP_SLUG` from that file too. For a different key mount, set `PRISM_PRIVATE_KEY_PATH` to the **container-side** path.

For a persistent systemd service, adapt [the example Quadlet](../deploy/web/prism.container) rather than installing it verbatim: change its host paths, env files and network to match your proxy. That example uses a shared container network rather than publishing port 8000.

### 4. Install and verify

Open the **install** URL printed by `convert.sh` and select just the repositories to review. Open a non-draft PR there, or as its author comment `@YOUR-APP-SLUG review` on an existing open PR. Use the actual slug GitHub returned, not the display name or the hosted bot's handle. Look for the `autoreviewer` check on the head commit; that's also the name to require in branch protection. If nothing appears, inspect the App's **Advanced → Recent deliveries** first: verify the delivery URL and response status before troubleshooting review or model logs.

### Migrate an existing receiver without losing reviews

Use a receiver build that supports the `PRISM_` settings before switching units. Keep the existing GitHub App, installation, webhook URL, and `autoreviewer` branch-protection check. **Do not run `convert.sh` again.**

1. Drain active reviews. Stop the old receiver (`systemctl --user stop autoreviewer.service` for Quadlet) and retire its old Quadlet so it cannot restart alongside Prism. Back up its credentials and its **entire** persistent volume while stopped, preserving ownership and permissions. Include `dbos.sqlite` and any SQLite sidecars, `steps/`, and `work/`. If any of those paths were overridden, back up the actual locations too.
2. Create `~/.config/prism` with mode 0700. Copy `autoreviewer-app.env` and `autoreviewer-app.pem` from the old credential directory (often `~/.config/homelab` or `~/.config/autoreviewer`) as `prism-app.env` and `prism-app.pem`. Keep the App ID, slug, webhook secret and key unchanged. Update `GITHUB_APP_PRIVATE_KEY_PATH` in the copied env file if it points to the old host path. Copy `autoreviewer.env` or `model.env` as `prism.env`; rename the legacy `AUTOREVIEWER_` prefix on receiver settings to `PRISM_` without changing credential values or `GITHUB_` keys. Keep credential files mode 0600. Copy the existing `prism.json`, or configure the same model pair before starting the new unit.
3. Copy the stopped state directory (formerly `~/.local/share/autoreviewer` by default) **with its contents** to `~/.local/share/prism`, or bind-mount the existing host directory directly at `/var/lib/prism`. Do not start against an empty database. Update custom `PRISM_DATABASE`, `PRISM_STEPS_DIR`, and `PRISM_WORK_DIR` values to their mounted paths.
4. Install the new `prism.container`, reload the user systemd manager, and point the shared-network proxy upstream at `prism:8000`. Start `prism.service` and verify `/healthz`, an existing review’s history, and a new GitHub delivery before retiring backups. To roll back, stop Prism first and restore the old unit, proxy target, credentials and matching pre-upgrade state snapshot.

If you also run sandboxd, drain VMs and stop and disable the old `autoreviewer-sandboxd.socket` and daemon before stopping the old egress unit. **Do not use the old uninstaller for migration:** it deletes the service account and VM state. Preserve the `smolrunner` UID and copy its stopped `/var/lib/smolrunner` tree to `/var/lib/prism/sandboxd` with ownership and mode intact; update the account’s home to that path. Reapply any local unit or firewall overrides under the new names, run `deploy/sandboxd/install.sh`, and point clients at `/run/prism/sandboxd.sock`. Run `deploy/sandboxd/verify.sh` before allowing new jobs.
