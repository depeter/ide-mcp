#!/usr/bin/env bash
# Unregister both servers from Codex and Claude Code. Pass --purge to also delete .venv.
#
#   scripts/uninstall.sh [--purge]
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PURGE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

for s in files kotlin; do
  if command -v codex >/dev/null 2>&1 && codex mcp remove "$s" >/dev/null 2>&1; then
    echo "Removed '$s' from Codex."
  fi
  if command -v claude >/dev/null 2>&1 && claude mcp remove --scope user "$s" >/dev/null 2>&1; then
    echo "Removed '$s' from Claude Code."
  fi
done
if [[ $PURGE -eq 1 ]]; then
  rm -rf "$DIR/.venv"
  echo "Deleted $DIR/.venv"
fi
