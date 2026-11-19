#!/bin/sh
# Updates home-web on the server, in one command:
#   1. code: git pull of /opt/home-web
#   2. house files: git pull of /opt/home-web-house, if it is a Git repository (private!)
#   3. rights (root:homeweb, nothing for others), systemd unit if it changed
#   4. check of the .tpy (errors and warnings only), restart, check
#
# Once: ln -sf /opt/home-web/tools/update.sh /usr/local/bin/home-web-update
# Then: home-web-update
set -eu
APP=/opt/home-web
HOUSE=/opt/home-web-house
PY=/opt/home-web-venv/bin/python

echo "== Code"
git -C "$APP" pull --ff-only
if [ -d "$HOUSE/.git" ]; then
    echo "== House files"
    git -C "$HOUSE" pull --ff-only
fi

chown -R root:homeweb "$APP" && chmod -R g+rX,o-rwx "$APP"
if [ -d "$HOUSE" ]; then chown -R root:homeweb "$HOUSE" && chmod -R g+rX,o-rwx "$HOUSE"; fi

if ! cmp -s "$APP/home-web.service" /etc/systemd/system/home-web.service; then
    cp "$APP/home-web.service" /etc/systemd/system/ && systemctl daemon-reload
    echo "== systemd unit updated"
fi

set -a; . /etc/home-web.env; set +a
if [ -n "${HOMEWEB_TPY:-}" ] && [ -f "$HOMEWEB_TPY" ]; then
    echo "== Checking $HOMEWEB_TPY"
    (cd "$APP" && "$PY" -m tools.check_tpy check "$HOMEWEB_TPY" | grep -E "^(ERROR|WARNING)") || echo "nothing to report"
fi

systemctl restart home-web
sleep 3
if systemctl is-active --quiet home-web; then
    echo "== home-web restarted"
    journalctl -u home-web -n 40 --no-pager | grep -E "Rules:|ERROR" || true
    echo "Reload the page in the browser (tabs already open keep the old version)."
else
    echo "!! home-web is not running:"
    journalctl -u home-web -n 30 --no-pager
    exit 1
fi
