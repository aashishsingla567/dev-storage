#!/usr/bin/env bash
# Optional non-destructive CPU/I/O fairness for T3's native agent scopes.
set -euo pipefail
if [[ $(systemctl --user show app-t3code-agents.slice -p LoadState --value) != loaded ]]; then
  echo 'T3 agent slice not loaded. Start an agent through T3 first.' >&2
  exit 1
fi
threads=$(getconf _NPROCESSORS_ONLN)
# Reserve approximately one CPU thread for the UI, SSH and host services.
quota=$(( (threads > 1 ? threads - 1 : 1) * 100 ))
systemctl --user set-property app-t3code-agents.slice "CPUQuota=${quota}%" CPUWeight=80 IOWeight=80
systemctl --user show app-t3code-agents.slice -p CPUQuotaPerSecUSec -p CPUWeight -p IOWeight -p MemoryHigh -p MemoryMax
echo 'Native T3 aggregate memory limits retained; no agents restarted.'
