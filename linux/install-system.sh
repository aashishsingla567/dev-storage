#!/usr/bin/env bash
# Optional administrator policy. No cache/snapshot deletion or application restart.
set -euo pipefail
if [[ $EUID != 0 ]]; then
  echo "Run with sudo: bash linux/install-system.sh" >&2
  exit 1
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP="/var/lib/dev-storage/backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP" /etc/systemd/journald.conf.d /etc/sysctl.d /usr/local/lib/dev-storage
chmod 0700 /var/lib/dev-storage /var/lib/dev-storage/backups "$BACKUP"
for file in /etc/systemd/journald.conf.d/60-dev-storage.conf /etc/sysctl.d/99-zz-dev-storage.conf; do
  if [[ -e $file ]]; then cp -a "$file" "$BACKUP/$(basename "$file")"; fi
done
sysctl vm.dirty_background_bytes vm.dirty_bytes vm.dirty_background_ratio vm.dirty_ratio > "$BACKUP/previous-sysctl.txt"
cat > /etc/systemd/journald.conf.d/60-dev-storage.conf <<'EOF'
[Journal]
SystemMaxUse=512M
RuntimeMaxUse=128M
SystemKeepFree=2G
MaxRetentionSec=14day
EOF
cat > /etc/sysctl.d/99-zz-dev-storage.conf <<'EOF'
# Bound writeback buffering on development hosts with concurrent builds.
vm.dirty_background_bytes=67108864
vm.dirty_bytes=536870912
EOF
sysctl -p /etc/sysctl.d/99-zz-dev-storage.conf
install -m 0644 "$ROOT/dev_storage_linux.py" /usr/local/lib/dev-storage/dev_storage_linux.py
cat > /etc/systemd/system/dev-storage-system-audit.service <<'EOF'
[Unit]
Description=Read-only system storage audit excluding encrypted-home backing files

[Service]
Type=oneshot
Environment=XDG_STATE_HOME=/var/lib
ExecStart=/usr/bin/python3 /usr/local/lib/dev-storage/dev_storage_linux.py audit --scheduled --root / --exclude /home/.ecryptfs
Nice=19
IOSchedulingClass=idle
CPUQuota=25%
MemoryHigh=512M
MemoryMax=1G
TimeoutStartSec=4h
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/lib/dev-storage
EOF
cat > /etc/systemd/system/dev-storage-system-audit.timer <<'EOF'
[Unit]
Description=Daily idle opportunity for weekly system storage audit

[Timer]
OnCalendar=*-*-* 03:00:00
RandomizedDelaySec=45min
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now dev-storage-system-audit.timer
echo "Backup: $BACKUP"
echo "Writeback limits applied. Journald limits apply after its next restart."
echo "No application services restarted and no files deleted."
