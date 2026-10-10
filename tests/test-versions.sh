#!/usr/bin/env bash
# plugin.json and capability.json must carry the same version.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
node -e '
  const r = (f) => JSON.parse(require("fs").readFileSync(process.argv[1] + "/" + f, "utf8")).version;
  const a = r(".claude-plugin/plugin.json"), b = r("capability.json");
  if (!a || a !== b) { console.error("FAIL: version mismatch: plugin.json=" + a + " capability.json=" + b); process.exit(1); }
  console.log("PASS: versions equal (" + a + ")");
' "$ROOT"
