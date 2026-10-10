#!/usr/bin/env bash
# URL import (`gsd capability install <git url>`) reads capability.json at the repo root,
# so the bundle must live there and survive a plain install of a repo copy.
# Needs gsd-tools (PATH or $CLAUDE_CONFIG_DIR/gsd-core) and node; run with an isolated HOME/GSD_HOME.
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "PASS: $1"; }

command -v node >/dev/null 2>&1 || fail "node required"
if command -v gsd-tools >/dev/null 2>&1; then GT=(gsd-tools)
elif [ -f "${CLAUDE_CONFIG_DIR:-${HOME:-}/.claude}/gsd-core/bin/gsd-tools.cjs" ]; then
  GT=(node "${CLAUDE_CONFIG_DIR:-${HOME:-}/.claude}/gsd-core/bin/gsd-tools.cjs")
else fail "gsd-tools not found"; fi

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH" 2>/dev/null' EXIT

[ -f "$REPO_ROOT/capability.json" ] && [ -f "$REPO_ROOT/scripts/pr_status.py" ] \
  || fail "capability.json and scripts/ are not at the repo root"
[ ! -e "$REPO_ROOT/.gsd" ] || fail "bundle still present under .gsd/"
pass "bundle sits at the repo root"

# A copy of the tracked tree stands in for the clone URL import makes.
COPY="$SCRATCH/copy"
mkdir -p "$COPY" "$SCRATCH/project/.planning" "$SCRATCH/home"
(cd "$REPO_ROOT" && git ls-files --cached --others --exclude-standard -z | tar --null -T - -cf -) | tar -x -C "$COPY"
printf '%s\n' '{}' > "$SCRATCH/project/.planning/config.json"
git -C "$SCRATCH/project" init -q

(cd "$SCRATCH/project" && GSD_HOME="$SCRATCH/home" "${GT[@]}" capability install "$COPY" --scope global --yes >/dev/null 2>&1 </dev/null) \
  || fail "capability install of the repo copy failed"

render() { (cd "$SCRATCH/project" && GSD_HOME="$SCRATCH/home" "${GT[@]}" loop render-hooks "$1" --raw 2>/dev/null </dev/null); }
# check <point> <js predicate over activeHooks filtered to capId pr-workflow>
check() {
  render "$1" | node -e '
    const h = JSON.parse(require("fs").readFileSync(0, "utf8")).activeHooks.filter((x) => x.capId === "pr-workflow");
    process.exit(eval(process.argv[1]) ? 0 : 1);' "$2"
}

check execute:wave:post 'h.length === 1 && h[0].kind === "step" && h[0].ref.skill === "pr-workflow-report" && h[0].produces.includes("PR.md")' \
  || fail "execute:wave:post does not route pr-workflow-report producing PR.md"
check ship:post 'h.length === 1 && h[0].kind === "step" && h[0].ref.skill === "pr-workflow-report"' \
  || fail "ship:post does not route pr-workflow-report"
check ship:pre 'h.length === 1 && h[0].kind === "gate" && h[0].blocking === false && h[0].check.predicate.artifact === "PR.md" && h[0].check.predicate.field === "pr_gate_ok"' \
  || fail "ship:pre advisory gate on PR.md pr_gate_ok not declared"
pass "global install routes both steps and declares the advisory ship:pre gate"

echo "ALL PASS"
