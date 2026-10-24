# dev-storage 🚀

A lightweight, zero-dependency developer storage and lifecycle management CLI for macOS.

**Linux devboxes:** a separate implementation provides allocated-byte audits,
advisory budgets, pressure-aware systemd timers, conservative cache retention,
and opt-in build/agent resource limits. See [Linux setup](linux/README.md).
On Linux, `./install.sh` installs that implementation with cleanup disabled.

It helps developers monitor, audit, and maintain fixed storage budgets for **iOS Simulator runtimes & devices**, **Android SDK system images & AVD emulators**, **Node.js/mise runtimes**, **package manager stores (npm/pnpm)**, and **local AI agent databases (OpenCode)**.

---

## 🧐 The Problem

Modern development toolchains silently accumulate dozens of gigabytes of obsolete data:
- **Xcode CoreSimulator**: Retains previous 8–15 GB iOS runtimes indefinitely after updates and creates 10–15 default device templates (~500 MB base system overlay each).
- **Android Studio / SDK**: Keeps legacy Android system images (~3.8 GB each) across API upgrades. Each active AVD emulator inflates to 8–15 GB with userdata and Play Store caches.
- **Node & Package Toolchains**: Multiple version managers leave abandoned Node versions (`~200 MB` each), and `~/.npm` acts as an append-only tarball cache.
- **AI Coding Agent Databases**: Local chat history and streaming token replay event streams expand to 10+ GB without periodic retention compaction.

`dev-storage` provides continuous visibility against strict allocation budgets and routine one-shot maintenance commands.

---

## ⚡ Installation

Clone and symlink to your local path:

```bash
git clone https://github.com/aashishsingla567/dev-storage.git ~/Development/tools/dev-storage
chmod +x ~/Development/tools/dev-storage/dev-storage
ln -sf ~/Development/tools/dev-storage/dev-storage ~/.local/bin/dev-storage
```

Or run the included installer:
```bash
./install.sh
```

Ensure `~/.local/bin` is in your `$PATH`.

---

## 🛠️ Usage

### 1. Storage & Allocation Audit
View current APFS partition usage and see real-time allocations against target budget ceilings:

```bash
dev-storage status
```

Example output:
```text
================================================================
 🚀 DEVELOPER STORAGE & ALLOCATION AUDIT (v1.0.0)
================================================================
 Drive Partition : 460Gi total | 12Gi used | 142Gi available (8% capacity)
----------------------------------------------------------------
 [iOS Simulators] Target Budget: ~10 GB | Current: 5.41 GB
   • Devices configured : 11
   • Installed runtime  : iOS 26.0 (26.0.1 - 23A8464)

 [Android Emulators & SDK] Target Budget: ~14 GB | Current: 11.81 GB
   • AVD user disks (~/.android/avd) : 8.03 GB
   • System Images (SDK)             : 3.78 GB

 [Node & Package Runtimes] Target Budget: ~8 GB | Current: 1.05 GB
   • Mise managed versions           : 594.4 MB
   • npm tarball cache (~/.npm)      : 478.6 MB
   • pnpm global store               : 0.0 KB

 [OpenCode Local Database] Target Budget: ~4 GB | Current: 4.20 GB
   • Total sessions: 313 (eligible for prune >60d: 0)
================================================================
```

---

### 2. Routine Maintenance Commands

#### Prune iOS Simulator Debris & Caches
Removes unavailable simulator devices referencing deleted runtimes and flushes `~/Library/Developer/CoreSimulator/Caches`:
```bash
dev-storage clean-sims
```

#### Wipe Android AVD User Bloat
Prints commands to shrink bloated 8–15 GB AVD snapshot disks back to ~300 MB without re-downloading the base system image:
```bash
dev-storage wipe-avd
```

#### Prune Unreferenced Runtimes & Package Stores
Runs `mise prune` to remove legacy Node/Python versions, cleans the npm cache, and prunes unreferenced pnpm store packages:
```bash
dev-storage clean-runtimes
```

#### Compact Agent History Databases
Prunes OpenCode chat sessions and orphaned event streams older than 60 days (customizable) and executes SQLite `VACUUM`:
```bash
dev-storage clean-opencode --days 60
```

#### Run All Safe Cleanups
Executes all safe maintenance routines in a single step:
```bash
dev-storage clean-all
```

---

## 🛡️ Recommended Guardrails

### Accidental `$HOME` Package Installation Guard
To prevent tools or scripts from accidentally creating `node_modules` or `package-lock.json` directly in your user root (`$HOME`), add this `package.json` to your `~` directory:

```json
{
  "name": "home-root-guard",
  "private": true,
  "scripts": {
    "preinstall": "echo '❌ ERROR: Cannot run package install directly in $HOME root directory! Please cd into your project directory.' && exit 1"
  }
}
```

---

## 📄 License

MIT License. Feel free to use, modify, and distribute.
