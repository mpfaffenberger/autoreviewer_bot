#!/bin/sh
# Remove everything install.sh created: services, firewall table, app, state, account and group.
#
#   sudo deploy/sandboxd/uninstall.sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo $0" >&2
    exit 1
fi

systemctl disable --now autoreviewer-sandboxd.socket autoreviewer-sandboxd.service \
    autoreviewer-sandbox-egress.service 2>/dev/null || true
rm -f /etc/systemd/system/autoreviewer-sandboxd.socket /etc/systemd/system/autoreviewer-sandboxd.service \
    /etc/systemd/system/autoreviewer-sandbox-egress.service
systemctl daemon-reload
nft delete table inet autoreviewer_sandbox 2>/dev/null || true
rm -rf /opt/autoreviewer-sandboxd /etc/autoreviewer-sandboxd /var/cache/autoreviewer-sandboxd-uv /var/lib/smolrunner
getent passwd smolrunner >/dev/null && userdel smolrunner
getent group sandboxd >/dev/null && groupdel sandboxd
echo "removed (left /usr/local/bin/uv in place)"
