#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
timers=true
init_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-timers) timers=false; shift ;;
    --enable-cleanup) init_args+=("$1"); shift ;;
    --profile) init_args+=("$1" "${2:?--profile requires heavy or balanced}"); shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done
LIB="${XDG_DATA_HOME:-$HOME/.local/share}/dev-storage"
BIN="$HOME/.local/bin"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$LIB" "$BIN" "$UNITS"
install -m 0644 "$ROOT/dev_storage_linux.py" "$LIB/dev_storage_linux.py"
install -m 0755 "$ROOT/linux/devbox-run" "$BIN/devbox-run"
# A copied launcher remains usable when the source checkout moves.
cat > "$BIN/dev-storage" <<EOF
#!/usr/bin/python3
import sys
import os
from pathlib import Path
sys.path.insert(0, str(Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "dev-storage"))
from dev_storage_linux import entrypoint
sys.exit(entrypoint())
EOF
chmod 0755 "$BIN/dev-storage"
"$BIN/dev-storage" init "${init_args[@]}"
if ! "$timers"; then
  echo 'Installed CLI without timers. Schedule dev-storage check / maintain with your init system.'
  exit 0
fi
install -m 0644 "$ROOT"/linux/systemd/* "$UNITS/"
systemctl --user daemon-reload
systemctl --user enable --now dev-storage-check.timer dev-storage-audit.timer dev-storage-maintain.timer
echo "Installed. Run dev-storage audit for the baseline; dev-storage clean previews expired caches."
