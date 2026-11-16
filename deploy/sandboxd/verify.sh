#!/bin/sh
# Acceptance checks for an installed sandboxd. Re-run after every install and every smolvm upgrade.
#
#   deploy/sandboxd/verify.sh        (as the user install.sh added to the sandboxd group)
#
# 1. Layer one: through the installed daemon, a sandbox reaches only its allowlisted hosts.
# 2. Layer two: with smolvm's allowlist OFF, the uid firewall alone keeps a VM off the LAN and host.
set -eu

REPO=$(cd "$(dirname "$0")/../.." && pwd)
UNIT_FILE=/etc/systemd/system/autoreviewer-sandboxd.service
IMAGE=$(sed -n 's/.*--permit-image \([^ ]*\).*/\1/p' "$UNIT_FILE" | head -n 1)
HOST_IP=$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')

echo "== 1/2 layer one: smolvm allowlist, through the installed daemon"
sg sandboxd -c "cd '$REPO' && SANDBOXD_SOCKET=/run/autoreviewer-sandboxd.sock SANDBOXD_TEST_IMAGE='$IMAGE' \
    uv run pytest -m smolvm -q -p no:cacheprovider -k 'allowlist or no_network'"

echo "== 2/2 layer two: uid firewall alone (smolvm allowlist off)"
unit="sbx-backstop-$$"
# Never --pipe here: under SELinux, handing our stdout to PID 1 once knocked systemd off D-Bus
# (EXECUTION.md, "Incident"). Output goes to the journal instead.
status=0
sudo systemd-run --wait --collect --quiet --unit="$unit" \
    -p User=smolrunner -p Group=smolrunner -p Environment=HOME=/var/lib/smolrunner -p RuntimeMaxSec=300 \
    /opt/autoreviewer-sandboxd/.venv/bin/python /opt/autoreviewer-sandboxd/backstop_probe.py "$IMAGE" "$HOST_IP" \
    || status=$?
sudo journalctl -u "$unit" -o cat --no-pager | grep BACKSTOP || true
exit "$status"
