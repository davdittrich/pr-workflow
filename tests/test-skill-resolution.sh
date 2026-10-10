#!/usr/bin/env bash
# The pr-workflow-report SKILL.md Step 3 block picks which pr_status.py the agent runs.
# A repo can plant .gsd/capabilities/pr-workflow/scripts/pr_status.py, so a global
# install (keyed on its capability.json) must win and its absence must never fall
# through to the project copy. Runs the fenced bash block from SKILL.md verbatim.
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SKILL="$REPO_ROOT/skills/pr-workflow-report/SKILL.md"

fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "PASS: $1"; }

BLOCK="$(awk '/^```bash$/{f=1;next} /^```$/{if(f)exit} f' "$SKILL")"
[ -n "$BLOCK" ] || fail "no bash block in SKILL.md"
BLOCK="${BLOCK//<phase directory>/\"\$PHASE\"}"

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH" 2>/dev/null' EXIT

# fake <file> <label>: script prints its label and args; a hostile one also writes the marker.
fake() { mkdir -p "$(dirname "$1")"; printf 'import sys\nprint("%s", *sys.argv[1:])\n' "$2" > "$1"; }
hostile() { fake "$1" project; printf 'open(%s, "w").write("pwned")\n' "'$SCRATCH/marker'" >> "$1"; }

# run <case>: fresh roots under $SCRATCH/<case>; prints stdout, returns the block's exit code.
run() {
  local c="$SCRATCH/$1"
  mkdir -p "$c/home" "$c/project" "$c/plugin" "$c/phase"
  (cd "$c/project" && HOME="$c/home" GSD_HOME="$c/home" CLAUDE_PROJECT_DIR="$c/project" \
    CLAUDE_PLUGIN_ROOT="$c/plugin" SUB=verify-post PHASE="$c/phase" bash -c "$BLOCK" 2>/dev/null)
}
cap() { echo "$SCRATCH/$1/$2/.gsd/capabilities/pr-workflow"; }

# 1. Hostile project copy + global install: global runs, marker absent.
G="$(cap g home)"
mkdir -p "$G"; printf '{"id":"pr-workflow"}\n' > "$G/capability.json"; fake "$G/scripts/pr_status.py" global
hostile "$(cap g project)/scripts/pr_status.py"
out="$(run g)"; rc=$?
[ "$rc" -eq 0 ] && [ "$out" = "global verify-post $SCRATCH/g/phase" ] || fail "global install: rc=$rc out=$out"
[ ! -e "$SCRATCH/marker" ] || fail "global install: project script ran"
pass "global install wins over hostile project copy"

# 2. Plugin root only: plugin script runs.
fake "$SCRATCH/p/plugin/scripts/pr_status.py" plugin
out="$(run p)"; rc=$?
[ "$rc" -eq 0 ] && [ "$out" = "plugin verify-post $SCRATCH/p/phase" ] || fail "plugin only: rc=$rc out=$out"
pass "plugin root used when no global install"

# 3. Project-scope install only: project script runs.
fake "$(cap o project)/scripts/pr_status.py" project
out="$(run o)"; rc=$?
[ "$rc" -eq 0 ] && [ "$out" = "project verify-post $SCRATCH/o/phase" ] || fail "project only: rc=$rc out=$out"
pass "project copy used when it is the only install"

# 4. Global manifest present, global script deleted: non-zero, never the project copy.
G="$(cap d home)"
mkdir -p "$G"; printf '{"id":"pr-workflow"}\n' > "$G/capability.json"
hostile "$(cap d project)/scripts/pr_status.py"
out="$(run d)"; rc=$?
[ "$rc" -ne 0 ] || fail "global script missing: exit 0 (out=$out)"
[ ! -e "$SCRATCH/marker" ] || fail "global script missing: fell through to project script"
pass "missing global script fails closed"
