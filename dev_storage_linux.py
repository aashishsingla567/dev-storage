"""Linux devbox accounting and conservative maintenance; Python standard library only."""
import argparse
from collections import Counter
from contextlib import contextmanager
import datetime as dt
import fcntl
import heapq
import json
import os
from pathlib import Path
import stat
import sys
import time

GIB = 1024 ** 3
HOME = Path.home()
STATE = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "dev-storage"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "dev-storage/config.json"
# Only disposable download caches/logs. Never browser profiles, agent state,
# dependencies, build trees, worktrees, databases, volumes, or credentials.
CACHE_PATHS = (".cache/pip", ".cache/uv", ".npm/_cacache", ".npm/_logs")
ACTIVE_TOOLS = {"npm", "pnpm", "yarn", "bun", "pip", "pip3", "uv"}
PREFIXES = {
    ".t3/runtime": "toolchains", ".t3/worktrees": "project-source-and-data",
    ".t3/tools": "toolchains", ".docker": "containers",
    ".android": "android-sdk-and-emulators", "Android": "android-sdk-and-emulators",
    ".local/bin": "toolchains", ".local/lib": "toolchains",
    ".t3/userdata/worktrees": "project-source-and-data",
    ".t3": "agent-state", ".codex": "agent-state", ".claude": "agent-state",
    ".gemini": "agent-state", ".openclaw": "agent-state",
    ".local/share/opencode": "agent-state", ".local/state/opencode": "agent-state",
    ".npm": "package-caches", ".pnpm-store": "package-caches",
    ".local/share/pnpm/store": "package-caches", ".cache/pip": "package-caches",
    ".cache/uv": "package-caches", ".cache/yarn": "package-caches",
    ".cache/Homebrew": "package-caches", ".cache/ms-playwright": "browser-runtimes",
    ".cache/puppeteer": "browser-runtimes", ".nvm": "toolchains",
    ".bun": "toolchains", ".local/share/mise": "toolchains",
    ".rustup": "toolchains", ".cargo": "toolchains", ".local/share/uv": "toolchains",
    ".cache/huggingface": "model-weights", ".ollama": "model-weights",
    ".local/share/containers": "containers", ".local/share/docker": "containers",
    "Downloads": "personal-downloads", "Documents": "personal-files",
    "Pictures": "personal-files", "Videos": "personal-files", "Music": "personal-files",
    ".local/share/Trash": "trash", ".cache": "other-caches",
    ".config": "application-state", ".cursor": "editors", ".vscode": "editors",
    ".local/share/flatpak": "desktop-apps", "snap": "desktop-apps",
}
SORTED_PREFIXES = sorted(PREFIXES, key=len, reverse=True)


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def lock(name="maintenance"):
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / f"{name}.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def filesystem(path):
    s = os.statvfs(path)
    total, free, available = (s.f_blocks * s.f_frsize, s.f_bfree * s.f_frsize,
                              s.f_bavail * s.f_frsize)
    return {"path": str(path), "device": os.stat(path).st_dev, "total": total,
            "used": total - free, "free": free, "available": available,
            "reserved_unavailable": free - available, "inodes": s.f_files,
            "inodes_used": s.f_files - s.f_ffree}


def category(path, home=HOME):
    try:
        rel = path.relative_to(home).as_posix()
    except ValueError:
        text = str(path)
        for prefix, kind in (("/timeshift", "system-snapshots"),
                             ("/var/lib/docker", "containers"),
                             ("/var/lib/containerd", "containers"),
                             ("/var/lib/libvirt", "virtual-machines"),
                             ("/var/log", "system-logs"), ("/var/cache", "system-caches"),
                             ("/home/.ecryptfs", "encrypted-home-backing"),
                             ("/home/linuxbrew", "toolchains"), ("/swapfile", "swap")):
            if text == prefix or text.startswith(prefix + "/"):
                return kind
        return "system-and-other-users"
    owner = None
    for prefix in SORTED_PREFIXES:
        if rel == prefix or rel.startswith(prefix + "/"):
            owner = PREFIXES[prefix]
            break
    if owner and owner != "project-source-and-data":
        return owner
    parts = path.relative_to(home).parts
    if "node_modules" in parts or ".venv" in parts or "venv" in parts:
        return "project-dependencies"
    if ".git" in parts:
        return "git-history"
    if any(p in {".next", ".nuxt", ".turbo", "target", ".gradle", "build", "dist"} for p in parts):
        return "build-artifacts"
    if owner == "project-source-and-data" or parts and parts[0] in {"Development", "refs", "agentic-box-setup", "tailnet-devbox"}:
        return "project-source-and-data"
    return "other-home-files"


def scan(roots, excludes=(), home=HOME, progress=False):
    """One pass, st_blocks accounting, same-device, no links, unique hardlinks.

    Categories are exclusive. Directory detail is hierarchical and must not be
    summed. For snapshots, shared inodes belong to the first encountered path.
    """
    seen = set()
    categories = Counter()
    directory_rows, largest, errors, skipped = [], [], [], []
    counts = Counter()
    excludes = {os.path.abspath(p) for p in excludes}
    last_progress = time.monotonic()

    def visit(path, device, depth=0):
        nonlocal last_progress
        allocated = apparent = 0
        if str(path) in excludes:
            skipped.append({"path": str(path), "reason": "explicit exclusion"})
            return allocated, apparent
        try:
            s = path.lstat()
            if s.st_dev != device:
                skipped.append({"path": str(path), "reason": "different filesystem"})
                return 0, 0
            if s.st_nlink > 1 and not stat.S_ISDIR(s.st_mode):
                key = s.st_dev, s.st_ino
                if key in seen:
                    counts["duplicate_hardlinks"] += 1
                    return 0, 0
                seen.add(key)
            allocated = s.st_blocks * 512
            apparent = s.st_size
            categories[category(path, home)] += allocated
            if progress and time.monotonic() - last_progress > 60:
                last_progress = time.monotonic()
                atomic_json(STATE / "audit-progress.json", {
                    "timestamp": stamp(), "complete": False, "current_path": str(path),
                    "counts": dict(counts), "partial_categories": dict(categories)})
                print(f"audit in progress: {counts['files']} files; {path}", file=sys.stderr, flush=True)
            if stat.S_ISDIR(s.st_mode):
                counts["directories"] += 1
                with os.scandir(path) as entries:
                    for entry in entries:
                        a, b = visit(Path(entry.path), device, depth + 1)
                        allocated += a
                        apparent += b
                if depth <= 3 or allocated >= 256 * 1024 ** 2:
                    directory_rows.append({"path": str(path), "allocated": allocated,
                                           "apparent": apparent, "depth": depth})
                if progress and depth == 1:
                    print(f"scanned {path}: {allocated / GIB:.2f} GiB", file=sys.stderr, flush=True)
            elif stat.S_ISREG(s.st_mode):
                counts["files"] += 1
                item = (allocated, str(path), apparent, s.st_mtime, s.st_nlink)
                if len(largest) < 100:
                    heapq.heappush(largest, item)
                elif item > largest[0]:
                    heapq.heapreplace(largest, item)
            else:
                counts["symlinks_and_special"] += 1
            return allocated, apparent
        except OSError as error:
            counts["errors"] += 1
            if len(errors) < 100:
                errors.append({"path": str(path), "error": str(error)})
            return allocated, apparent

    totals = []
    for root in roots:
        root = Path(os.path.abspath(root))
        a, b = visit(root, root.lstat().st_dev)
        totals.append({"path": str(root), "allocated": a, "apparent": b})
    return {"timestamp": stamp(), "roots": totals, "categories": dict(categories),
            "directories": sorted(directory_rows, key=lambda x: -x["allocated"]),
            "largest_files": [{"allocated": a, "path": p, "apparent": b,
                               "mtime": m, "links": n} for a, p, b, m, n in sorted(largest, reverse=True)],
            "counts": dict(counts), "errors": errors, "skipped": skipped,
            "notes": ["Categories are additive; directory rows overlap.",
                      "Live scan is not an atomic snapshot; permissions and concurrent changes are reported.",
                      "Separate encrypted/plaintext mounts can refer to the same physical data; never sum them."]}


def memory():
    result = {}
    with open("/proc/meminfo") as stream:
        for line in stream:
            key, rest = line.split(":", 1)
            result[key] = int(rest.split()[0]) * 1024
    return result


def resources():
    mem = memory()
    pressure = {}
    for kind in ("cpu", "memory", "io"):
        try:
            pressure[kind] = Path(f"/proc/pressure/{kind}").read_text().strip()
        except OSError:
            pass
    return {"cpu_threads": os.cpu_count(), "load": os.getloadavg(),
            "memory_total": mem["MemTotal"], "memory_available": mem["MemAvailable"],
            "swap_total": mem["SwapTotal"], "swap_used": mem["SwapTotal"] - mem["SwapFree"],
            "pressure": pressure}


def defaults():
    total = filesystem(HOME)["total"] / GIB
    # Allocation envelopes, not quotas or a promise that arbitrary workloads fit.
    fs = filesystem(HOME)
    # Leave room for the free-space target AND blocks unavailable to this user.
    usable_budget = max(0, fs["total"] * .80 - fs["reserved_unavailable"]) / GIB
    factor = min(total / 468, usable_budget / 370)
    base = {"os-apps": 35, "system-snapshots": 30, "swap": 8,
            "project-source-and-data": 55, "git-history": 15,
            "project-dependencies": 45, "build-artifacts": 20,
            "toolchains": 20, "package-caches": 8, "other-caches": 5,
            "agent-state": 12, "containers": 25, "browser-runtimes": 4,
            "application-state": 10, "personal-files-and-downloads": 30,
            "system-logs-and-caches": 3, "model-weights": 0,
            "virtual-machines": 0, "android-sdk-and-emulators": 0,
            "encrypted-overhead-and-other": 45}
    ram = memory()["MemTotal"] / GIB
    cpu = os.cpu_count() or 1
    active_agents = max(1, min(cpu // 2, int(max(1, ram - 6) // 2)))
    return {"version": 1, "cleanup_enabled": False, "cache_retention_days": 30,
            "minimum_available_gib": round(total * .20, 1),
            "critical_available_gib": round(total * .10, 1),
            "budgets_gib": {k: round(v * factor, 1) for k, v in base.items()},
            "workload": {"active_agents": active_agents, "parallel_builds": 1,
                         "open_lightweight_agents": max(1, min(8, int(max(1, ram - 7) // 2))),
                         "build_memory_gib": 4, "agent_memory_gib_each": 1.5,
                         "minimum_available_ram_gib": 2},
            "audit_roots": [str(HOME)]}


def config():
    data = defaults()
    if CONFIG.exists():
        data.update(json.loads(CONFIG.read_text()))
    if not isinstance(data["cleanup_enabled"], bool):
        raise ValueError("cleanup_enabled must be boolean")
    for key in ("cache_retention_days",):
        if not isinstance(data[key], (int, float)) or data[key] < 1:
            raise ValueError(f"{key} must be at least 1")
    return data


def busy_reasons():
    r = resources()
    reasons = []
    if r["load"][0] > max(1, r["cpu_threads"] * .75):
        reasons.append("CPU/load pressure")
    if r["memory_available"] < max(2 * GIB, r["memory_total"] * .15):
        reasons.append("low available memory")
    for kind in ("memory", "io"):
        for line in r["pressure"].get(kind, "").splitlines():
            fields = line.split()
            if fields[0] == "full" and float(fields[1].split("=")[1]) > 2:
                reasons.append(f"{kind} stall pressure")
    return reasons


def active_cache_users():
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid == os.getuid():
                name = (proc / "comm").read_text().strip()
                cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                if (name.split()[0] in ACTIVE_TOOLS or
                        any(token in cmd for token in ("npm-cli.js", "pnpm.cjs", "yarn.js", "-m pip "))):
                    return True
        except OSError:
            pass
    return False


def candidate_files(home, days, now=None):
    cutoff = (time.time() if now is None else now) - days * 86400
    for rel in CACHE_PATHS:
        root = home / rel
        # No parent symlinks, even if they point to another disposable tree.
        if any(p.is_symlink() for p in (root, *list(root.parents)[:-1])) or not root.is_dir():
            continue
        for parent, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(parent) / d).is_symlink()]
            for name in names:
                path = Path(parent) / name
                try:
                    s = path.lstat()
                    if (stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid()
                            and s.st_nlink == 1 and max(s.st_atime, s.st_mtime, s.st_ctime) < cutoff):
                        yield root, path, s
                except OSError:
                    continue


def unlink_checked(root, path, expected):
    """Anchor every directory with O_NOFOLLOW; reject replaced/touched files."""
    parts = path.absolute().parts
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        current = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        attributes = ("st_dev", "st_ino", "st_mtime_ns", "st_ctime_ns", "st_atime_ns", "st_size", "st_nlink")
        if not stat.S_ISREG(current.st_mode) or any(getattr(current, k) != getattr(expected, k) for k in attributes):
            raise RuntimeError("file changed after planning")
        os.unlink(parts[-1], dir_fd=fd)
    finally:
        os.close(fd)


def clean(cfg, apply=False):
    result = {"timestamp": stamp(), "apply": apply, "planned_bytes": 0,
              "removed_bytes": 0, "files": 0, "errors": [], "skipped": [], "caches": {}}
    if apply:
        reasons = busy_reasons()
        if active_cache_users():
            reasons.append("development runtimes active; cache may be in use")
        if reasons:
            result["skipped"] = reasons
            return result
    for root, path, s in candidate_files(HOME, cfg["cache_retention_days"]):
        summary = result["caches"].setdefault(str(root), {"files": 0, "planned_bytes": 0, "removed_bytes": 0})
        summary["files"] += 1
        summary["planned_bytes"] += s.st_blocks * 512
        result["planned_bytes"] += s.st_blocks * 512
        result["files"] += 1
        if apply:
            try:
                unlink_checked(root, path, s)
                result["removed_bytes"] += s.st_blocks * 512
                summary["removed_bytes"] += s.st_blocks * 512
            except (OSError, RuntimeError) as error:
                result["errors"].append({"path": str(path), "error": str(error)})
    return result


def check(cfg):
    volumes = [filesystem("/"), filesystem(HOME)]
    r = resources()
    warnings = []
    for volume in volumes:
        if volume["available"] < cfg["critical_available_gib"] * GIB:
            warnings.append(f'{volume["path"]}: CRITICAL available below emergency floor')
        elif volume["available"] < cfg["minimum_available_gib"] * GIB:
            warnings.append(f'{volume["path"]}: available below free-space target')
        if volume["inodes"] and volume["inodes_used"] / volume["inodes"] > .8:
            warnings.append(f'{volume["path"]}: inode usage exceeds 80%')
    warnings.extend(busy_reasons())
    snapshot_dir = Path("/timeshift/snapshots")
    snapshot_health = {}
    if snapshot_dir.is_dir():
        try:
            dates = []
            for info in snapshot_dir.glob("*/info.json"):
                data = json.loads(info.read_text())
                dates.append(float(data["created"]))
            snapshot_health = {"count": len(dates), "newest_created": max(dates) if dates else None}
            if not dates or time.time() - max(dates) > 14 * 86400:
                warnings.append("Timeshift recovery snapshot missing or older than 14 days")
        except (OSError, ValueError, KeyError) as error:
            snapshot_health = {"error": str(error)}
            warnings.append("Timeshift snapshot freshness could not be checked")
    audit_path = STATE / "latest-audit.json"
    allocations = {}
    if audit_path.exists():
        audit = json.loads(audit_path.read_text())
        allocations = audit.get("categories", {})
        for name, size in allocations.items():
            budget = cfg["budgets_gib"].get(name)
            if budget is not None and size > budget * GIB:
                warnings.append(f"{name}: above {budget} GiB advisory budget (cached audit)")
        age = time.time() - audit_path.stat().st_mtime
        if age > 8 * 86400:
            warnings.append("deep audit older than 8 days")
    else:
        warnings.append("no deep audit available")
    return {"timestamp": stamp(), "filesystems": volumes, "resources": r,
            "warnings": warnings, "cleanup_enabled": cfg["cleanup_enabled"],
            "cached_allocations": allocations, "timeshift": snapshot_health}


def entrypoint():
    try:
        return main()
    except (OSError, ValueError) as error:
        print(f"dev-storage: {error}", file=sys.stderr)
        return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Write adaptive budgets; preserves existing config")
    init.add_argument("--enable-cleanup", action="store_true")
    init.add_argument("--profile", choices=("heavy", "balanced"))
    audit = sub.add_parser("audit", help="Deep read-only allocated/apparent accounting")
    audit.add_argument("--root", action="append", default=[])
    audit.add_argument("--exclude", action="append", default=[])
    audit.add_argument("--output")
    audit.add_argument("--scheduled", action="store_true")
    for name in ("status", "check", "maintain", "clean"):
        command = sub.add_parser(name)
        if name == "clean":
            command.add_argument("--apply", action="store_true", help="Otherwise only plan")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.command == "init":
        cfg = config()
        if args.profile:
            cfg["workload"].update(defaults()["workload"])
            cfg["workload"]["profile"] = args.profile
            if args.profile == "balanced":
                cfg["workload"]["open_lightweight_agents"] = cfg["workload"]["active_agents"]
        if args.enable_cleanup:
            cfg["cleanup_enabled"] = True
        atomic_json(CONFIG, cfg)
        print(f"Configuration: {CONFIG}; cleanup_enabled={cfg['cleanup_enabled']}")
        return 0
    cfg = config()
    if args.command in {"status", "check"}:
        result = check(cfg)
        atomic_json(STATE / "latest-check.json", result)
        print(json.dumps(result, indent=2))
        return 1 if result["warnings"] else 0
    with lock("audit" if args.command == "audit" else "maintenance"):
        if args.command == "audit":
            latest = STATE / "latest-audit.json"
            if args.scheduled and latest.exists() and time.time() - latest.stat().st_mtime < 7 * 86400:
                print(json.dumps({"skipped": ["audit less than seven days old"], "timestamp": stamp()}))
                return 0
            if args.scheduled and busy_reasons():
                print(json.dumps({"skipped": busy_reasons(), "timestamp": stamp()}))
                return 0
            roots = [Path(p).expanduser().absolute() for p in (args.root or cfg["audit_roots"])]
            # Avoid duplicate accounting from explicitly overlapping roots.
            for i, root in enumerate(roots):
                if any(root == other or root in other.parents or other in root.parents
                       for other in roots[i + 1:]):
                    raise ValueError("audit roots must not overlap")
            result = scan(roots, args.exclude, progress=True)
            result["resources"] = resources()
            result["filesystems"] = [filesystem(p) for p in roots]
            result["budgets_gib"] = cfg["budgets_gib"]
            output = Path(args.output) if args.output else STATE / "latest-audit.json"
            atomic_json(output, result)
            atomic_json(STATE / "audit-progress.json", {"timestamp": stamp(), "complete": True,
                                                       "output": str(output), "counts": result["counts"]})
            print(f"Audit saved: {output}; {result['counts'].get('errors', 0)} scan errors")
            return 1 if result["counts"].get("errors") else 0
        if args.command == "clean":
            result = clean(cfg, args.apply)
        else:
            result = {"check": check(cfg), "timestamp": stamp()}
            result["cleanup"] = clean(cfg, True) if cfg["cleanup_enabled"] else {"skipped": ["cleanup disabled"]}
        atomic_json(STATE / "latest-maintenance.json", result)
        print(json.dumps(result, indent=2))
        return 1 if result.get("errors") or result.get("cleanup", {}).get("errors") else 0


if __name__ == "__main__":
    sys.exit(entrypoint())
