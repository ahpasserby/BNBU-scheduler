#!/bin/bash
set -euo pipefail
if [[ $EUID -ne 0 || $# -ne 1 ]]; then
    echo 'Usage: sudo bash install-agent.sh /path/to/checkout' >&2
    exit 2
fi
PRINT_SOURCE="$(cd -- "$1" && pwd)"
test -f "$PRINT_SOURCE/campus_print/agent.py"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends python3 smbclient ghostscript poppler-utils bubblewrap openssh-client openssl ca-certificates
# Office documents and images are converted to PDF in the same offline sandbox.
DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui libreoffice-draw-nogui fonts-noto-cjk fonts-crosextra-carlito fonts-crosextra-caladea fonts-liberation2
id maxcourse-print >/dev/null 2>&1 || useradd --system --home-dir /var/lib/maxcourse-print-agent --shell /usr/sbin/nologin maxcourse-print
install -d -m 0755 /opt/maxcourse-print-agent
install -d -m 0700 /etc/maxcourse-print-agent
install -d -o maxcourse-print -g maxcourse-print -m 0700 /var/lib/maxcourse-print-agent
cp -R "$PRINT_SOURCE/campus_print" /opt/maxcourse-print-agent/
chown -R root:root /opt/maxcourse-print-agent
chmod -R go-w /opt/maxcourse-print-agent
if [[ ! -f /etc/maxcourse-print-agent/agent.env ]]; then
    python3 - <<'PY'
from pathlib import Path
import os
import secrets
p=Path('/etc/maxcourse-print-agent/agent.env')
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as out:
    out.write('MAXCOURSE_PRINT_AGENT_TOKEN='+secrets.token_urlsafe(48)+'\n')
    out.write('PRINT_AGENT_STATE_DIR=/var/lib/maxcourse-print-agent\nPRINT_AGENT_RUNTIME_DIR=/tmp/maxcourse-print-agent\n')
PY
fi
# Migrate only the old default, leaving custom paths and secrets untouched.
sed -i 's|^PRINT_AGENT_RUNTIME_DIR=/run/maxcourse-print-agent$|PRINT_AGENT_RUNTIME_DIR=/tmp/maxcourse-print-agent|' /etc/maxcourse-print-agent/agent.env
if [[ ! -f /var/lib/maxcourse-print-agent/agent.key ]]; then
    openssl req -x509 -newkey rsa:3072 -nodes -days 730 \
        -subj '/CN=maxcourse-print-agent' \
        -addext 'subjectAltName=IP:127.0.0.1,IP:::1' \
        -keyout /var/lib/maxcourse-print-agent/agent.key \
        -out /var/lib/maxcourse-print-agent/agent.crt >/dev/null 2>&1
    chown maxcourse-print:maxcourse-print /var/lib/maxcourse-print-agent/agent.key /var/lib/maxcourse-print-agent/agent.crt
    chmod 0600 /var/lib/maxcourse-print-agent/agent.key
    chmod 0644 /var/lib/maxcourse-print-agent/agent.crt
fi
test -f /var/lib/maxcourse-print-agent/agent.crt
install -m 0644 "$PRINT_SOURCE/deploy/print-portal/maxcourse-print-agent.service" /etc/systemd/system/
install -m 0644 "$PRINT_SOURCE/deploy/print-portal/maxcourse-print-tunnel.service" /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/maxcourse-print-agent.service /etc/systemd/system/maxcourse-print-tunnel.service
systemctl daemon-reload
systemctl enable --now maxcourse-print-agent.service
echo 'Agent installed on loopback. Configure the restricted tunnel and cloud token before enabling portal submissions.'
