#!/usr/bin/env bash
set -u

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

bash "$PLUGIN_ROOT"/hooks/capability-auto-install.sh pr-workflow || true
