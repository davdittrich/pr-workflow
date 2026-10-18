#!/usr/bin/env bash
# The pr-workflow-report SKILL.md Step 3 block picks which pr_status.py the agent runs.
# A repo can plant .gsd/capabilities/pr-workflow/scripts/pr_status.py, so a global
# install (keyed on its capability.json) must win and its absence must never fall
# through to the project copy; the project copy is never a resolution rung. Runs the
# fenced bash block from SKILL.md verbatim.
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

# run <case> [VAR=value...]: fresh roots under $SCRATCH/<case>; trailing assignments override
# the defaults, UNSET=<var> drops one. Prints stdout, writes stderr to $SCRATCH/<case>/stderr,
# returns the block's exit code.
run() {
  local c="$SCRATCH/$1"; shift
  mkdir -p "$c/home" "$c/project" "$c/plugin" "$c/phase"
  (cd "$c/project" && env HOME="$c/home" GSD_HOME="$c/home" \
    CLAUDE_PROJECT_DIR="$c/project" CLAUDE_PLUGIN_ROOT="$c/plugin" SUB=verify-post \
    PHASE="$c/phase" "$@" bash -c "${UNSET:+unset $UNSET;}$BLOCK" 2>"$c/stderr")
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

# 2. Plugin root + hostile project copy, no global install: plugin runs, marker absent.
fake "$SCRATCH/p/plugin/scripts/pr_status.py" plugin
hostile "$(cap p project)/scripts/pr_status.py"
out="$(run p)"; rc=$?
[ "$rc" -eq 0 ] && [ "$out" = "plugin verify-post $SCRATCH/p/phase" ] || fail "plugin only: rc=$rc out=$out"
[ ! -e "$SCRATCH/marker" ] || fail "plugin only: project script ran"
pass "plugin root wins over hostile project copy"

# 3. Neither global install nor plugin root, hostile project copy: exit 1 with the not-found
# message, project script never runs (no project rung).
hostile "$(cap o project)/scripts/pr_status.py"
run o >/dev/null; rc=$?; err="$(<"$SCRATCH/o/stderr")"
[ "$rc" -eq 1 ] || fail "project only: rc=$rc (want 1)"
case "$err" in *"pr_status.py not found in global or plugin roots"*) ;; *) fail "project only: message=$err";; esac
[ ! -e "$SCRATCH/marker" ] || fail "project only: project script ran"
pass "project copy never runs; missing global and plugin fails closed with message"

# 3b. Relative CLAUDE_PLUGIN_ROOT resolves against the project cwd: a planted ./scripts copy must not run.
hostile "$SCRATCH/rp/project/scripts/pr_status.py"
run rp CLAUDE_PLUGIN_ROOT=. >/dev/null; rc=$?; err="$(<"$SCRATCH/rp/stderr")"
[ "$rc" -eq 1 ] || fail "relative plugin root: rc=$rc (want 1)"
case "$err" in *"pr_status.py not found in global or plugin roots"*) ;; *) fail "relative plugin root: message=$err";; esac
[ ! -e "$SCRATCH/marker" ] || fail "relative plugin root: cwd script ran"
pass "relative CLAUDE_PLUGIN_ROOT ignored; fails closed with message"

# 4. Global manifest present, global script deleted: non-zero, never the project copy.
G="$(cap d home)"
mkdir -p "$G"; printf '{"id":"pr-workflow"}\n' > "$G/capability.json"
hostile "$(cap d project)/scripts/pr_status.py"
out="$(run d)"; rc=$?
[ "$rc" -ne 0 ] || fail "global script missing: exit 0 (out=$out)"
[ ! -e "$SCRATCH/marker" ] || fail "global script missing: fell through to project script"
pass "missing global script fails closed"

# 5. GSD_HOME unset: global install under HOME wins over hostile project copy.
G="$(cap h home)"
mkdir -p "$G"; printf '{"id":"pr-workflow"}\n' > "$G/capability.json"; fake "$G/scripts/pr_status.py" global
hostile "$(cap h project)/scripts/pr_status.py"
out="$(UNSET=GSD_HOME run h)"; rc=$?
[ "$rc" -eq 0 ] && [ "$out" = "global verify-post $SCRATCH/h/phase" ] || fail "HOME fallback: rc=$rc out=$out"
[ ! -e "$SCRATCH/marker" ] || fail "HOME fallback: project script ran"
pass "GSD_HOME unset falls back to HOME"

# closed <case> <label> [VAR=value...]: hostile project copy present; expect non-zero, marker absent.
closed() {
  local c="$1" label="$2"; shift 2
  hostile "$(cap "$c" project)/scripts/pr_status.py"
  out="$(run "$c" "$@")"; rc=$?
  [ "$rc" -ne 0 ] || fail "$label: exit 0 (out=$out)"
  [ ! -e "$SCRATCH/marker" ] || fail "$label: fell through to project script"
  pass "$label fails closed"
}

# 6-7. No usable base dir: HOME empty or unset, GSD_HOME unset.
UNSET=GSD_HOME closed e "HOME empty" HOME=
UNSET=HOME closed u "HOME unset" GSD_HOME=

# 8. Relative GSD_HOME resolves against the project cwd; a planted manifest must not make it global.
printf '{"id":"pr-workflow"}\n' > "$(mkdir -p "$(cap r project)" && cap r project)/capability.json"
closed r "relative GSD_HOME" GSD_HOME=.

# 9. Dangling-symlink manifest still counts as a global install.
G="$(cap s home)"; mkdir -p "$G"; ln -s "$G/nowhere" "$G/capability.json"
closed s "dangling manifest symlink"

# 10. Directory where the manifest should be still counts as a global install.
mkdir -p "$(cap m home)/capability.json"
closed m "directory manifest"
