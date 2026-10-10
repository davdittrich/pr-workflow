#!/usr/bin/env bash
# hooks/capability-auto-install.sh behaviour tests. gsd-tools is always a spy, never real.
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ID=pr-workflow

fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "PASS: $1"; }

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH" 2>/dev/null' EXIT

# mk_env <name>: plugin copy, GSD_HOME, spy on PATH, non-git cwd.
mk_env() {
  E="$SCRATCH/$1"
  PLUGIN="$E/plugin"; HOME_DIR="$E/home"; SPY="$E/spy"; CALLS="$E/calls"; CWD="$E/cwd"
  mkdir -p "$PLUGIN/hooks" "$HOME_DIR" "$SPY" "$CWD" "$E/claude"
  (cd "$REPO_ROOT" && cp -R capability.json skills scripts tests "$PLUGIN"/)
  cp "$REPO_ROOT/hooks/capability-auto-install.sh" "$PLUGIN/hooks/"
  : > "$CALLS"
  printf '#!/usr/bin/env bash\necho "$@" >> "%s"\nexit 0\n' "$CALLS" > "$SPY/gsd-tools"
  chmod +x "$SPY/gsd-tools"
  HASH="$HOME_DIR/.gsd/capability-auto-install-$ID.hash"
}

run_hook() {
  (cd "$CWD" && PATH="$SPY:$PATH" CLAUDE_CONFIG_DIR="$E/claude" CLAUDE_PLUGIN_ROOT="$PLUGIN" GSD_HOME="$HOME_DIR" \
    bash "$PLUGIN/hooks/capability-auto-install.sh" "$ID" 2>"$E/stderr" >"$E/stdout"; echo $?)
}
ncalls() { wc -l < "$CALLS" | tr -d ' '; }

case1() {
  mk_env c1
  [ "$(run_hook)" = 0 ] || fail "baseline: first run exit != 0"
  [ "$(ncalls)" = 1 ] || fail "baseline: expected 1 spy call, got $(ncalls)"
  grep -q "^capability install $HOME_DIR/.gsd/capability-bundle-$ID --scope global --yes\$" "$CALLS" || fail "baseline: unexpected arguments: $(cat "$CALLS")"
  [ -s "$HASH" ] || fail "baseline: hash sidecar not written"
  grep -q "Auto-installed capability: $ID" "$E/stdout" || fail "baseline: success notice missing"
  [ ! -e "$HOME_DIR/.gsd/capability-bundle-$ID/tests" ] || fail "baseline: staged tests/"
  [ "$(run_hook)" = 0 ] || fail "baseline: second run exit != 0"
  [ ! -s "$E/stdout" ] && [ ! -s "$E/stderr" ] || fail "baseline: second run not silent"
  [ "$(ncalls)" = 1 ] || fail "baseline: second run called spy again"
}

# Hash covers only the bundle: tests/ and __pycache__ must not re-install; skills/ must.
case2() {
  mk_env c2
  run_hook >/dev/null
  echo x >> "$PLUGIN/tests/test_pr_status.py"
  mkdir -p "$PLUGIN/scripts/__pycache__"; echo x > "$PLUGIN/scripts/__pycache__/a.pyc"
  echo x > "$PLUGIN/README.md"
  [ "$(run_hook)" = 0 ] || fail "hash scope: exit != 0"
  [ "$(ncalls)" = 1 ] || fail "hash scope: non-bundle change re-installed"
  echo '<!-- edit -->' >> "$PLUGIN/skills/pr-workflow-report/SKILL.md"
  [ "$(run_hook)" = 0 ] || fail "hash scope: exit != 0 after skill edit"
  [ "$(ncalls)" = 2 ] || fail "hash scope: skill edit did not re-install"
}

case3() {
  mk_env c3
  rm -rf "$PLUGIN/skills"
  [ "$(run_hook)" = 0 ] || fail "staging failure: exit != 0"
  [ "$(ncalls)" = 0 ] || fail "staging failure: spy called"
  [ ! -e "$HASH" ] || fail "staging failure: hash written"
  grep -q 'staging failed' "$E/stderr" || fail "staging failure: stderr lacks 'staging failed'"
}

# SECURITY: a repo-local gsd-core/bin/gsd-tools.cjs in the session cwd must never run.
case4() {
  mk_env c4
  marker="$E/hostile-ran"
  git -C "$CWD" init -q
  mkdir -p "$CWD/gsd-core/bin"
  printf 'require("fs").writeFileSync(%s,"x")\n' "'$marker'" > "$CWD/gsd-core/bin/gsd-tools.cjs"
  rm -f "$SPY/gsd-tools"
  [ "$(run_hook)" = 0 ] || fail "hostile: exit != 0"
  [ ! -e "$marker" ] || fail "hostile: repo-local gsd-tools.cjs was executed"
  [ ! -s "$E/stderr" ] && [ ! -s "$E/stdout" ] || fail "hostile: not silent"
}

# Executable surface: no --yes, one stderr review line, no state.
case5() {
  mk_env c5
  node -e 'const f=process.argv[1],m=JSON.parse(require("fs").readFileSync(f));m.hooks=[{event:"SessionStart",script:"x.sh"}];require("fs").writeFileSync(f,JSON.stringify(m))' "$PLUGIN/capability.json"
  [ "$(run_hook)" = 0 ] || fail "executable: exit != 0"
  [ "$(ncalls)" = 1 ] || fail "executable: expected 1 spy call, got $(ncalls)"
  grep -q -- '--yes' "$CALLS" && fail "executable: --yes passed"
  grep -q "capability install $HOME_DIR/.gsd/capability-bundle-$ID" "$E/stderr" || fail "executable: stderr lacks review line"
  [ ! -e "$HASH" ] || fail "executable: hash written"
}

# The hook never reads the session's stdin.
case6() {
  mk_env c6
  printf '#!/usr/bin/env bash\necho "$@" >> "%s"\nread -r l && printf "%%s" "$l" > "%s"\nexit 0\n' "$CALLS" "$E/stdin" > "$SPY/gsd-tools"
  [ "$(printf 'y\n' | run_hook)" = 0 ] || fail "stdin: exit != 0"
  [ "$(ncalls)" = 1 ] || fail "stdin: spy not called"
  [ ! -s "$E/stdin" ] || fail "stdin: spy read session stdin"
}

# No gsd resolver: silent, exit 0, no state, no staging.
case7() {
  mk_env c7
  rm -f "$SPY/gsd-tools"
  [ "$(run_hook)" = 0 ] || fail "no resolver: exit != 0"
  [ ! -s "$E/stderr" ] && [ ! -s "$E/stdout" ] || fail "no resolver: not silent"
  [ ! -e "$HASH" ] || fail "no resolver: hash written"
}

# Real install failure: stderr message, no state, no success notice.
case8() {
  mk_env c8
  printf '#!/usr/bin/env bash\necho "$@" >> "%s"\nexit 3\n' "$CALLS" > "$SPY/gsd-tools"
  [ "$(run_hook)" = 0 ] || fail "install failure: exit != 0"
  grep -q 'install failed.*exit 3' "$E/stderr" || fail "install failure: stderr lacks message"
  [ ! -s "$E/stdout" ] || fail "install failure: printed success notice"
  [ ! -e "$HASH" ] || fail "install failure: hash written"
}

# Notice only after the state file is written (state path blocked by a directory).
case9() {
  mk_env c9
  mkdir -p "$HASH"
  [ "$(run_hook)" = 0 ] || fail "state write: exit != 0"
  [ ! -s "$E/stdout" ] || fail "state write: notice printed although state not written"
}

# No home at all: silent exit 0.
case10() {
  mk_env c10
  local out
  out="$(cd "$CWD" && env -i PATH="$SPY:$PATH" CLAUDE_PLUGIN_ROOT="$PLUGIN" \
    bash "$PLUGIN/hooks/capability-auto-install.sh" "$ID" 2>"$E/stderr"; echo "rc=$?")"
  [ "$out" = "rc=0" ] || fail "no home: got $out"
  [ ! -s "$E/stderr" ] || fail "no home: stderr not empty"
  [ "$(ncalls)" = 0 ] || fail "no home: spy called"
}

rc=0
for n in 1 2 3 4 5 6 7 8 9 10; do
  ( "case$n" ) >"$SCRATCH/out$n" 2>&1 && pass "case $n" || { cat "$SCRATCH/out$n"; rc=1; }
done
[ "$rc" = 0 ] && echo "ALL PASS"
exit "$rc"
