#!/usr/bin/env bash
# Build the virtualenv and register both servers with Codex (and optionally Claude Code).
#
#   scripts/install.sh               build + register "files" and "kotlin" with Codex
#   scripts/install.sh --claude      also register with Claude Code (user scope)
#   scripts/install.sh --no-codex    build only (combine with --claude if wanted)
#   scripts/install.sh --only kotlin register just one of the two servers (files | kotlin)
#   scripts/install.sh --with-lsp    also download JetBrains' Kotlin language server (370 MB) for
#                                    code-aware find usages, rename and IntelliJ inspections
#
# Safe to re-run: it replaces any existing registration of the same names.
set -euo pipefail

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    echo "On Windows use the PowerShell script instead:" >&2
    echo "  powershell -ExecutionPolicy Bypass -File scripts\\install.ps1" >&2
    exit 1 ;;
esac

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DO_CODEX=1
DO_CLAUDE=0
SERVERS=(files kotlin)
WITH_LSP=0
KOTLIN_TIMEOUT=1800

while [[ $# -gt 0 ]]; do
  case "$1" in
    --claude) DO_CLAUDE=1; shift ;;
    --no-codex) DO_CODEX=0; shift ;;
    --with-lsp) WITH_LSP=1; shift ;;
    --only)
      case "${2:-}" in files|kotlin) SERVERS=("$2") ;; *) echo "--only needs 'files' or 'kotlin'" >&2; exit 2 ;; esac
      shift 2 ;;
    -h|--help) sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

echo "==> Building virtualenv in $DIR/.venv"
if command -v uv >/dev/null 2>&1; then
  (cd "$DIR" && uv sync --quiet --no-dev)
elif command -v python3 >/dev/null 2>&1; then
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || { echo "Python 3.10+ is required (or install uv: https://docs.astral.sh/uv/)." >&2; exit 1; }
  python3 -m venv "$DIR/.venv"
  "$DIR/.venv/bin/pip" install --quiet --upgrade pip
  "$DIR/.venv/bin/pip" install --quiet -e "$DIR"
else
  echo "Need either uv or python3 (3.10+) on PATH." >&2
  exit 1
fi

for s in "${SERVERS[@]}"; do
  [[ -x "$DIR/.venv/bin/$s-mcp" ]] || { echo "Build did not produce $DIR/.venv/bin/$s-mcp; not registering anything." >&2; exit 1; }
done

# Agents start MCP servers with a trimmed environment, so pin what Gradle needs instead of
# relying on the shell profile at launch time.
JAVA="${JAVA_HOME:-}"
if [[ -z "$JAVA" ]] && command -v java >/dev/null 2>&1; then
  JAVA="$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")"
fi
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
if [[ -z "$SDK" ]]; then
  for c in "$HOME/Android/Sdk" "$HOME/android-sdk" "$HOME/Library/Android/sdk"; do
    if [[ -d "$c" ]]; then SDK="$c"; break; fi
  done
fi
KOTLIN_ENV=()
[[ -n "$JAVA" && -x "$JAVA/bin/java" ]] && KOTLIN_ENV+=(--env "JAVA_HOME=$JAVA")
[[ -n "$SDK" ]] && KOTLIN_ENV+=(--env "ANDROID_HOME=$SDK")
[[ ${#KOTLIN_ENV[@]} -eq 0 ]] && echo "!!  No JDK found; Gradle tools will fail until JAVA_HOME is set (JAVA_HOME=/path $0)." >&2

if [[ $WITH_LSP -eq 1 ]]; then
  echo "==> Installing the Kotlin language server"
  "$DIR/.venv/bin/kotlin-mcp" --install-lsp
fi

echo "==> Self-check"
for s in "${SERVERS[@]}"; do
  JAVA_HOME="${JAVA:-}" ANDROID_HOME="${SDK:-}" "$DIR/.venv/bin/$s-mcp" --check || true
done

env_args() {  # prints nothing; fills ENV_ARGS for server $1
  ENV_ARGS=()
  if [[ "$1" == kotlin ]]; then ENV_ARGS=(${KOTLIN_ENV[@]+"${KOTLIN_ENV[@]}"}); fi
}

if [[ $DO_CODEX -eq 1 ]]; then
  if command -v codex >/dev/null 2>&1; then
    for s in "${SERVERS[@]}"; do
      echo "==> Registering '$s' with Codex"
      env_args "$s"
      codex mcp remove "$s" >/dev/null 2>&1 || true
      codex mcp add "$s" ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} -- "$DIR/.venv/bin/$s-mcp"
    done
    # Codex kills tool calls after 60s by default, which no real Gradle build survives.
    CONFIG="${CODEX_HOME:-$HOME/.codex}/config.toml"
    if [[ " ${SERVERS[*]} " == *" kotlin "* && -f "$CONFIG" ]]; then
      "$DIR/.venv/bin/python" - "$CONFIG" "$KOTLIN_TIMEOUT" <<'PY'
import re, sys
path, timeout = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
m = re.search(r"^\[mcp_servers\.kotlin\][ \t]*\n", text, re.M)
if m:
    nxt = re.search(r"^\[(?!mcp_servers\.kotlin[.\]])", text[m.end():], re.M)
    section = text[m.end(): m.end() + nxt.start()] if nxt else text[m.end():]
    if "tool_timeout_sec" not in section:
        text = text[:m.end()] + f"tool_timeout_sec = {timeout}\n" + text[m.end():]
        open(path, "w", encoding="utf-8").write(text)
        print(f"    set tool_timeout_sec = {timeout} for 'kotlin' in {path}")
else:
    print(f"!!  [mcp_servers.kotlin] not found in {path}; add tool_timeout_sec = {timeout} to it yourself.")
PY
    fi
  else
    echo "==> codex CLI not found. Add this to ~/.codex/config.toml yourself:"
    for s in "${SERVERS[@]}"; do
      echo
      echo "[mcp_servers.$s]"
      echo "command = \"$DIR/.venv/bin/$s-mcp\""
      if [[ "$s" == kotlin ]]; then
        echo "tool_timeout_sec = $KOTLIN_TIMEOUT"
        echo "env = { JAVA_HOME = \"$JAVA\", ANDROID_HOME = \"$SDK\" }"
      fi
    done
    echo
  fi
fi

if [[ $DO_CLAUDE -eq 1 ]]; then
  if command -v claude >/dev/null 2>&1; then
    for s in "${SERVERS[@]}"; do
      echo "==> Registering '$s' with Claude Code (user scope)"
      env_args "$s"
      claude mcp remove --scope user "$s" >/dev/null 2>&1 || true
      claude mcp add --scope user "$s" ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} -- "$DIR/.venv/bin/$s-mcp"
    done
  else
    echo "!!  claude CLI not found; skipped." >&2
  fi
fi

echo
echo "Done. Start a new agent session to pick up: ${SERVERS[*]}"
