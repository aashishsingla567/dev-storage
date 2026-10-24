# Linux devbox storage and workload maintenance

Linux uses a separate standard-library Python implementation; the existing
macOS commands remain available on macOS. Requires Python 3.10+, systemd user
services, and a unified cgroup v2 hierarchy for optional workload limits.
No third-party Python packages, database migrations, or daemon are needed.
On Linux without systemd, `./install.sh --no-timers` installs the audit CLI;
schedule its commands with the host's own scheduler. `devbox-run` requires
systemd and cgroups even when the CLI is installed without timers.

```bash
./install.sh
dev-storage init --profile heavy     # advisory CPU/RAM-based concurrency plan
dev-storage audit                    # deep, read-only home scan
dev-storage status                   # cheap capacity/pressure + cached budgets
dev-storage clean                    # expired-cache plan, no deletion
dev-storage clean --apply            # explicit cleanup; defers under pressure
dev-storage init --enable-cleanup    # opt in to scheduled cache cleanup
devbox-run build -- npm run build    # opt-in resource limits
devbox-run agent -- codex            # opt-in resource limits
```

The installer preserves existing configuration and copies the utility into
`$XDG_DATA_HOME/dev-storage` (default `~/.local/share/dev-storage`). The source
checkout can move afterward. Settings are in
`$XDG_CONFIG_HOME/dev-storage/config.json`; reports are in
`$XDG_STATE_HOME/dev-storage` (private directories/files, 0700/0600). Reports
include paths and sizes; they do not read file contents or store credentials.
Keep host-specific reports out of public repositories.

## Accounting and budgets

`audit` counts allocated filesystem blocks and apparent sizes in one pass,
deduplicates hardlinks, avoids symlink traversal and other-device mounts,
reports skipped paths/errors, lists the 100 largest files, and records
directory totals through depth three and larger directories at any depth.
Categories are exclusive and additive. Directory totals are nested and overlap.
Hardlink blocks belong to whichever path is visited first; individual snapshot
or project sizes are therefore traversal-dependent. Sum the exclusive totals.
Sparse files can have huge apparent sizes and small allocated sizes.

On eCryptfs, plaintext `stat` can open and decrypt every file header, making
large dependency trees expensive to audit. Optional backing metadata mode
matches decrypted directory entries to raw backing files by inode and counts
their allocated blocks without reading those headers:

```bash
dev-storage audit --encrypted-backing-root /home/.ecryptfs/USERNAME/.Private
```

The backing and mounted roots must have matching inode numbers on different
filesystems. This is read-only and never exposes encrypted filenames in the
report. Aggregate apparent sizes are `null` in this mode; the largest files
still get apparent sizes from plaintext paths. Unmatched entries during live
renames/mount changes are reported as errors, not guessed. Configure
`encrypted_backing_root` in the private config for scheduled home audits.
Only use the raw backing directory of this mounted eCryptfs home; this option
is not a generic encrypted-filesystem scanner.

For a privileged system scan, choose a private output explicitly:

```bash
sudo python3 dev_storage_linux.py audit --root / \
  --exclude /home/.ecryptfs --exclude /timeshift \
  --output /root/dev-storage-system.json
```

An eCryptfs home and its backing directory represent the same physical files.
Audit the mounted home for meaningful paths and the root filesystem separately;
never add decrypted-home blocks to a root total that includes the backing files.
Use `df`/`statvfs` for actual available capacity, including reserved blocks.
For rsync snapshots, scan all snapshots together to deduplicate their hardlinks.
Files shared between the live system and snapshots require a combined scan to
attribute unique physical blocks. `df - du` includes metadata, reservations,
unreadable paths, deleted-open files and changes made during the scan; it is
not automatically recoverable space.

Budgets are advisory allocation envelopes, scaled from a 468 GiB reference
filesystem, with 20% available space as the target and 10% as critical. Review
the generated config for each host. Categories absent from the budget map are
still reported. Aggregate system/personal envelopes require review of the
system and home reports; they are not enforced quotas. `status` compares only
exact matching home category names and warns when the cached audit is stale.
Generated capacity budgets do not make a small disk or large project fit.
No code is automatically deleted to meet a budget.

For a four-thread / 16 GiB host, start with two actively working agents, one
heavy build, at least 2 GiB available RAM, and eight GiB swap. A model request
waiting on a remote provider differs from a local compiler, browser, or test
worker: count active local work rather than just open conversations. Use one
shared package store and one or two maintained toolchain versions. Avoid local
model weights and VMs on this profile unless you explicitly allocate space.

`devbox-run` limits new commands only. Builds have 3 GiB memory high / 4 GiB
hard limit; agents 1.5 / 2.5 GiB. Each gets up to two CPU threads, 1 GiB swap,
and 256 tasks. Aggregate opted-in builds are limited to 6 GiB RAM and two CPU
threads; agents to 5 GiB and two threads. These are **ceilings, not reserved
memory**. Hitting a hard limit can terminate that workload. Existing T3 sessions
and desktop apps are not moved into these slices. For memory-heavy builds,
adjust the launcher/slice before using it. CLI stdin/TTY and current directory
are preserved by `systemd-run --scope`.
When launched inside a T3 agent scope, the command stays under T3's native
aggregate agent slice as well, so the wrapper cannot escape T3's host budget.
Its scope binds to the originating agent scope so stopping that agent also
stops the wrapped workload.
Standalone wrappers and T3 share the same physical RAM: their separate ceilings
are not an admission controller. Keep one heavy build at a time on a 16 GiB host.

For T3's existing Linux agent scopes, optionally run
`bash linux/configure-t3-limits.sh`. This persistently caps aggregate agent CPU
at approximately all but one host thread and sets CPU/I/O weight to 80. T3's
native memory-high/max policy is retained; the UI/server is in a separate
service. This affects existing agent scope descendants without restarting
them. CPU quota bounds CPU time, not worker count, and I/O weight support
depends on the block scheduler. Revert with `systemctl --user set-property
app-t3code-agents.slice CPUQuota=infinity CPUWeight=100 IOWeight=100`.

## Scheduled work

- Hourly capacity/pressure check (plus a randomized delay of up to 5 minutes).
- Daily 04:00 opportunity, randomized up to 45 minutes, for an audit when the
  previous audit is at least seven days old. Busy hosts defer to another day.
- Sunday 05:00 maintenance, randomized up to 45 minutes. Cleanup defaults off.

Deep scans and cleanup defer if one-minute load exceeds 75% of CPU thread
count, available RAM is below 2 GiB or 15%, or full memory/I/O PSI avg10
exceeds 2%. Cleanup also defers while a same-user package manager is running.
The idle check occurs before work; systemd also caps CPU/memory and lowers I/O
priority. Latest reports replace previous reports, so scheduler output does
not accumulate unlimited report files. Review warnings through:

```bash
dev-storage status
systemctl --user list-timers 'dev-storage*'
journalctl --user -u dev-storage-check -u dev-storage-maintain -u dev-storage-audit
cat ~/.local/state/dev-storage/latest-check.json
```

Checks return 1 for warnings, 2 for execution errors. The check treats
snapshot age over fourteen days as a warning when Timeshift snapshots are
present. This detects stale recovery points without creating or pruning them.
The systemd check treats
1 as a completed check; warnings remain in its output and private JSON. There
is no external alert delivery. A user manager must run for timers to run;
`loginctl show-user "$USER" -p Linger` shows whether they survive logout.
On a dedicated remote host, an administrator can enable lingering with
`sudo loginctl enable-linger "$USER"`. The installer does not change it.

## Cleanup boundary

Only regular, single-link, current-user files under `.cache/pip`, `.cache/uv`,
`.npm/_cacache`, and `.npm/_logs` can be removed. Atime, mtime **and ctime** must
all be older than the retention window (default 30 days). Parent symlinks,
symlinked files, recently touched/replaced files, and hardlinks are excluded.
Deletion uses directory descriptors with `O_NOFOLLOW`; failures are reported.
Expired cache cleanup is not guaranteed to reclaim enough space on a busy host.
Do not run another user's package installation concurrently with cleanup on
shared stores. This setup manages per-user caches only.

Never automatically remove source, worktrees, `node_modules`, build output,
downloads, browser state, agent history, credentials, SDKs, Docker volumes,
databases, or Timeshift snapshots. Review those separately with their owner
and native tool. Avoid blind `docker system prune -a --volumes` and SQL DELETE
or VACUUM against live agent databases. Use supported exports, consistent
backups, and retention controls provided by each application.

System logs, APT and Timeshift are administrator-owned. Existing logrotate,
tmpfiles and TRIM timers should be checked before adding more jobs. Suggested
journald ceiling for this profile: `SystemMaxUse=512M`, `RuntimeMaxUse=128M`,
`SystemKeepFree=2G`, `MaxRetentionSec=14day`. These caps do not replace the
20% filesystem target. Snapshot target: 30 GiB, keep two recent successful
snapshots plus an off-machine recovery path. Never delete the last recovery
snapshot just to hit a budget. Timeshift is not a backup of your code or agent
state; verify exclusions and maintain an encrypted off-machine backup.

## Removal and validation

```bash
systemctl --user disable --now dev-storage-{check,audit,maintain}.timer
rm ~/.config/systemd/user/dev-storage-*.{service,timer}
systemctl --user daemon-reload
python3 -m unittest discover -s tests -v
```

Removing the timers leaves the config, reports and CLI available. Remove the
two devbox slice files only after all opted-in scopes finish. No existing
application services, sleep settings, filesystem reservations, or network
settings are changed by this installer.

Investigate a persistent `df`/`du` gap for files hidden beneath mounts. A mounted home can conceal an old
home directory that still occupies the root filesystem. Use the read-only
private-namespace helper to inspect it without unmounting the running home:

```bash
sudo python3 linux/audit-underlay /home/USERNAME /root/hidden-home.json
```

The optional administrator installer also installs this as
`/usr/local/sbin/dev-storage-underlay`. It binds a root view read-only inside a
private mount namespace, audits the underlying directory, then unmounts only
that temporary view. Hidden files may be unique historical data; archive and
compare them before any deletion. This helper never deletes files and is not
run automatically. Its output uses `under-mount:` labels, not live paths.

On a large ext4 root partition, review the root-only reserved block pool
separately from used data: the traditional 5% can reserve over 20 GiB on a
500 GB drive. Reducing it to 1% can be reasonable when this still leaves
several GiB for recovery. Inspect `sudo tune2fs -l /dev/DEVICE` and save the
original reserved block count; `sudo tune2fs -m 1 /dev/DEVICE` changes the
percentage online. Restore the exact count with `tune2fs -r ORIGINAL_COUNT`.
This changes user-available capacity without deleting data or increasing
physical free blocks. Never apply it blindly to an unknown filesystem or a
small root volume. None of the installers changes filesystem reservations.

For optional administrator policies, run `sudo bash linux/install-system.sh`.
It backs up prior managed files and current dirty-write settings under
`/var/lib/dev-storage/backups/`, applies 64 MiB background / 512 MiB maximum
dirty-write buffers, writes the journald caps above, and installs a weekly
system audit with a daily idle opportunity at 03:00–03:45. System reports live
at `/var/lib/dev-storage/latest-audit.json` and exclude `/home/.ecryptfs` to
avoid counting encrypted-home data twice. Journald config applies on its next
restart. Reinstall after updating the Python implementation to update its
root-owned copy. This script is optional and requires systemd and procfs.

To roll back administrator policies, disable `dev-storage-system-audit.timer`,
remove its two units and the two managed policy files, restore previous files
from the backup if present, and run `systemctl daemon-reload`. Restore the four
recorded dirty-write settings with `sysctl` (ratios and byte limits are mutually
exclusive: apply the previously nonzero pair last). The journal drop-in removal
takes effect at its next restart. Never restore unrelated global sysctl files.
