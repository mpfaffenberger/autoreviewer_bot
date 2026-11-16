#!/bin/sh
# Install or update sandboxd on this host. Idempotent.
#
#   sudo deploy/sandboxd/install.sh
#
# Creates the `smolrunner` account and the `sandboxd` socket group (and adds the invoking sudo user
# to it), installs a root-owned copy of the app under /opt, loads the egress firewall, and starts the
# service. Undo with deploy/sandboxd/uninstall.sh.
set -eu

REPO=$(cd "$(dirname "$0")/../.." && pwd)
PREFIX=/opt/autoreviewer-sandboxd
UV_SOURCE=${UV:-$(command -v uv || true)}

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo $0" >&2
    exit 1
fi
if [ ! -c /dev/kvm ]; then
    echo "/dev/kvm is missing: smolvm needs KVM" >&2
    exit 1
fi

getent group sandboxd >/dev/null || groupadd --system sandboxd
getent passwd smolrunner >/dev/null || useradd --system --user-group --no-create-home \
    --home-dir /var/lib/smolrunner --shell /usr/sbin/nologin --comment "autoreviewer sandboxd" smolrunner
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    usermod --append --groups sandboxd "$SUDO_USER"
fi

# uv for root's build of the app (the invoking user's copy lives in a home root can read, smolrunner can't).
if [ ! -x /usr/local/bin/uv ]; then
    if [ -z "$UV_SOURCE" ]; then
        echo "uv not found; set UV=/path/to/uv" >&2
        exit 1
    fi
    install -m 0755 "$UV_SOURCE" /usr/local/bin/uv
fi

# Root-owned and read-only to smolrunner, so a guest that escapes its VM can't rewrite the daemon.
install -d -m 0755 "$PREFIX"
rm -rf "$PREFIX/src"
cp -R "$REPO/src" "$PREFIX/src"
install -m 0644 "$REPO/pyproject.toml" "$REPO/uv.lock" "$REPO/EXECUTION.md" \
    "$REPO/deploy/sandboxd/backstop_probe.py" "$PREFIX/"
(
    cd "$PREFIX"
    UV_PYTHON_INSTALL_DIR="$PREFIX/python" UV_PYTHON_PREFERENCE=only-managed \
        UV_CACHE_DIR=/var/cache/autoreviewer-sandboxd-uv \
        /usr/local/bin/uv sync --frozen --no-dev --extra sandboxd --no-editable --python 3.13 --quiet
)
chown -R root:root "$PREFIX"
chmod -R u=rwX,go=rX "$PREFIX"

install -d -m 0755 /etc/autoreviewer-sandboxd
install -m 0644 "$REPO/deploy/sandboxd/egress.nft" /etc/autoreviewer-sandboxd/egress.nft
install -m 0644 "$REPO/deploy/sandboxd/autoreviewer-sandbox-egress.service" \
    "$REPO/deploy/sandboxd/autoreviewer-sandboxd.service" \
    "$REPO/deploy/sandboxd/autoreviewer-sandboxd.socket" /etc/systemd/system/

systemctl daemon-reload
systemctl enable autoreviewer-sandbox-egress.service autoreviewer-sandboxd.socket
systemctl restart autoreviewer-sandbox-egress.service  # reload the rules on update
systemctl stop autoreviewer-sandboxd.service           # the next connection starts the new code
systemctl restart autoreviewer-sandboxd.socket
systemctl --no-pager --lines=0 status autoreviewer-sandboxd.socket
