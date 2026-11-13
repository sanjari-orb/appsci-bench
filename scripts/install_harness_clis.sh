#!/usr/bin/env bash
# Install pinned CLIs without sudo or changes to global CLI/auth configuration.
# Local: bash scripts/install_harness_clis.sh [--check]
# Image: APPSCI_CLI_PREFIX=/opt/harness-clis bash scripts/install_harness_clis.sh
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo_root/harnesses/versions.env"
cli_prefix="${APPSCI_CLI_PREFIX:-$repo_root/.harness-clis}"
export PATH="$cli_prefix/bin:$PATH"
mode="${1:-install}"
case "$mode" in
    install|--check) ;;
    *) echo 'Usage: bash scripts/install_harness_clis.sh [--check]' >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || exit 2

matches() {
    local output
    output="$("$1" --version 2>/dev/null)" || return 1
    case "$1" in
        codex) [ "$output" = "codex-cli $2" ] ;;
        claude) [ "$output" = "$2 (Claude Code)" ] ;;
    esac
}

packages=()
matches codex "$CODEX_VERSION" || packages+=("@openai/codex@$CODEX_VERSION")
matches claude "$CLAUDE_CODE_VERSION" || packages+=("@anthropic-ai/claude-code@$CLAUDE_CODE_VERSION")
if [ "${#packages[@]}" -gt 0 ]; then
    if [ "$mode" = "--check" ]; then
        echo "[harness] Missing pinned CLIs: ${packages[*]}" >&2
        exit 1
    fi
    if ! command -v npm >/dev/null 2>&1 || ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)' 2>/dev/null; then
        echo '[harness] Install Node.js 22+ and npm, then retry (or use the Docker image).' >&2
        exit 1
    fi
    # Claude's native binary needs optional dependencies and its postinstall script.
    npm install --global --prefix "$cli_prefix" --cache "$cli_prefix/npm-cache" \
        --include=optional --ignore-scripts=false --no-audit --no-fund "${packages[@]}"
fi
matches codex "$CODEX_VERSION" && matches claude "$CLAUDE_CODE_VERSION" || {
    echo '[harness] CLI version verification failed.' >&2
    exit 1
}
echo "[harness] Codex $CODEX_VERSION; Claude Code $CLAUDE_CODE_VERSION ready (no login performed)."
