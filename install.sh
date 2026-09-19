#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_BIN="$HOME/.local/bin"

mkdir -p "$TARGET_BIN"
chmod +x "$DIR/dev-storage"
ln -sf "$DIR/dev-storage" "$TARGET_BIN/dev-storage"

echo "✅ dev-storage installed successfully to $TARGET_BIN/dev-storage"
echo "Run 'dev-storage status' to get started."
