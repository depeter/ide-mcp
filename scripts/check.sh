#!/usr/bin/env bash
# Show what each server sees: tool count, JDK, Android SDK, lint CLIs.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -x "$DIR/.venv/bin/kotlin-mcp" ]] || { echo "Not installed yet. Run scripts/install.sh first." >&2; exit 1; }
"$DIR/.venv/bin/files-mcp" --check
echo
"$DIR/.venv/bin/kotlin-mcp" --check
