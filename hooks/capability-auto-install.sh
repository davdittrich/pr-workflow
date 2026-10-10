#!/usr/bin/env bash
# Auto-install hook: re-grants the pr-workflow capability at global ("user")
# scope when the bundle changed. Bundle files sit at the plugin root so that
# `gsd capability install <git url>` also works (URL import reads
# capability.json at the repo root). Only the bundle files are hashed and
# staged, never the rest of the checkout (.git, hooks, tests, symlinks).
# Never aborts the session: no `set -e`.
set -u

CAP_ID="${1:-}"

# Defense in depth: validate the id shape gsd-core enforces before it reaches any path.
[[ "$CAP_ID" =~ ^[a-z][a-z0-9-]*$ ]] || exit 0

# No home dir to install under; `set -u` would abort on the paths below.
[ -n "${GSD_HOME:-${HOME:-}}" ] || exit 0

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
BUNDLE_SRC="$PLUGIN_ROOT"
[ -f "$BUNDLE_SRC/capability.json" ] || exit 0
BUNDLE_FILES=(capability.json skills scripts)

# Portable hash tool selection (macOS ships no sha256sum).
if command -v sha256sum >/dev/null 2>&1; then
  HASH_CMD=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
  HASH_CMD=(shasum -a 256)
else
  exit 0
fi

# Bundle hash: sorted paths (files and directories) of the bundle files, then
# one digest per sorted regular file. __pycache__ is excluded: running
# pr_status.py from an installed copy creates it and must not look like drift.
bundle_hash() {
  {
    (cd "$BUNDLE_SRC" && find "${BUNDLE_FILES[@]}" -name __pycache__ -prune -o \( -type f -o -type d \) -print 2>/dev/null) | LC_ALL=C sort
    (cd "$BUNDLE_SRC" && find "${BUNDLE_FILES[@]}" -name __pycache__ -prune -o -type f -print 2>/dev/null) | LC_ALL=C sort | while IFS= read -r _f; do (cd "$BUNDLE_SRC" && "${HASH_CMD[@]}" "$_f"); done
  } | "${HASH_CMD[@]}" | awk '{print $1}'
}

# One sidecar per capability id; never gsd-core's own .gsd-capabilities.json
# or ~/.gsd/consent.json.
STATE_FILE="${GSD_HOME:-$HOME}/.gsd/capability-auto-install-$CAP_ID.hash"

OLD_HASH=""
[ -r "$STATE_FILE" ] && OLD_HASH="$(cat "$STATE_FILE" 2>/dev/null)"
NEW_HASH="$(bundle_hash)"

# Fast path: unchanged bundle exits silently, never spawns node.
[ "$NEW_HASH" = "$OLD_HASH" ] && exit 0

# Never resolve gsd-tools from the cwd's repo: a cloned repo's
# gsd-core/bin/gsd-tools.cjs is untrusted code and must not run at SessionStart.
if command -v gsd-tools >/dev/null 2>&1; then
  GSD_TOOLS=(gsd-tools)
elif [ -f "${CLAUDE_CONFIG_DIR:-${HOME:-}/.claude}/gsd-core/bin/gsd-tools.cjs" ]; then
  GSD_TOOLS=(node "${CLAUDE_CONFIG_DIR:-${HOME:-}/.claude}/gsd-core/bin/gsd-tools.cjs")
else
  # This plugin does nothing without gsd: stay silent every session.
  exit 0
fi

# Stage the bundle files into a persistent clean directory; gsd-core records the
# install source, so it must outlive this run. A failed stage exits before the
# install and before the hash is recorded.
BUNDLE_DIR="${GSD_HOME:-$HOME}/.gsd/capability-bundle-$CAP_ID"
if ! { rm -rf "$BUNDLE_DIR" && mkdir -p "$BUNDLE_DIR" && (cd "$BUNDLE_SRC" && cp -RL "${BUNDLE_FILES[@]}" "$BUNDLE_DIR"/) && find "$BUNDLE_DIR" -name __pycache__ -prune -exec rm -rf {} + ; }; then
  echo "capability-auto-install: staging failed for $CAP_ID; not installed" >&2
  exit 0
fi

# Spec is the absolute staged dir. --yes only for a declarative bundle: gsd-core
# counts hooks, commands, mcpServers and reviewer as executable surfaces
# (src/capability-trust.cts hasExecutable); those need the user's own consent.
# Unparseable manifest = executable (fail closed). </dev/null: a prompt must
# never block session start.
if node -e '
  const m = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
  const arr = (v) => Array.isArray(v) && v.length > 0;
  const obj = (v) => v !== null && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length > 0;
  process.exit(arr(m.hooks) || arr(m.commands) || arr(m.mcpServers) || obj(m.mcpServers) || obj(m.reviewer) ? 1 : 0);
' "$BUNDLE_DIR/capability.json" >/dev/null 2>&1; then
  "${GSD_TOOLS[@]}" capability install "$BUNDLE_DIR" --scope global --yes </dev/null >/dev/null 2>&1
  INSTALL_STATUS=$?
else
  # gsd-core discloses and aborts without --yes. No hash write: re-reminded each session.
  "${GSD_TOOLS[@]}" capability install "$BUNDLE_DIR" --scope global </dev/null >/dev/null 2>&1
  echo "capability-auto-install: $CAP_ID declares executable surfaces, not auto-consented; review with: gsd-tools capability install $BUNDLE_DIR --scope global" >&2
  exit 0
fi

if [ "$INSTALL_STATUS" -eq 0 ]; then
  # Atomic state write; announce only once the state is recorded.
  if mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null \
    && printf '%s' "$NEW_HASH" > "$STATE_FILE.tmp.$$" 2>/dev/null \
    && mv -f "$STATE_FILE.tmp.$$" "$STATE_FILE" 2>/dev/null \
    && [ "$(cat "$STATE_FILE" 2>/dev/null)" = "$NEW_HASH" ]; then
    printf 'Auto-installed capability: %s (user scope)\n' "$CAP_ID"
  else
    rm -f "$STATE_FILE.tmp.$$" 2>/dev/null
  fi
else
  echo "capability-auto-install: capability install failed for $CAP_ID (exit $INSTALL_STATUS)" >&2
fi

exit 0
