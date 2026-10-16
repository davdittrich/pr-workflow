# pr-workflow

gsd-core capability: reads the current branch's PR and check status through `gh`.

## Behavior

- **`execute:wave:post`** runs `scripts/pr_status.py verify-post <phase_dir>`. Each run overwrites
  `{phase_dir}/{NN}-PR.md` from live `gh` data. Frontmatter: `pr_status`
  (`none`, `passing`, `pending`, `failing`, `unavailable`) and `pr_gate_ok` (true for `none` and
  `passing`).
- **`ship:post`** runs `ship-post-notice` and prints a notice when the branch has no open PR. It
  reads and writes no file.
- **`ship:pre`** is an advisory gate on `PR.md`. When `PR.md` is missing or `pr_gate_ok` is not
  `true`, the gate shows its message and ship continues (`blocking: false`).

If `gh`, `git` or the PR read fails, the script prints a notice, writes the `unavailable` report
(`pr_gate_ok: false`) and exits 0, so a report from an earlier run never survives a failed read.
Only when that report cannot be written does the script delete the old `PR.md` and exit 1.

## Config

Keys live in `.planning/config.json` under `"pr-workflow"`:

| Key | Default | Effect |
|-----|---------|--------|
| `enabled` | `true` | `false` skips both steps. |
| `ship_gate` | `true` | `false` removes the `ship:pre` gate. |

## Requirements

- gsd-core >= 1.6.0
- `gh`, authenticated (`gh auth login`), and `git` on `PATH`
- Python 3.9+, standard library only (the suite also passes on 3.8)

The script only reads: `gh pr list` and `gh pr checks`.

## Install

```bash
claude plugin marketplace add davdittrich/gsd-beads
claude plugin install pr-workflow@gsd-beads -y
```

A `SessionStart` hook (`startup`, `resume`) installs the bundle (`capability.json`, `skills/`,
`scripts/`) at global scope when it changed, and stays silent when gsd-core is absent. It passes
`--yes` only for manifests with no hooks, commands, MCP servers or reviewer.

## Uninstall

```bash
claude plugin uninstall pr-workflow -y
```

## Script resolution

`pr_status.py` resolves from the global manifest, then `CLAUDE_PLUGIN_ROOT`. With neither, the skill exits 1 (`pr_status.py not found in global or plugin roots`); it never runs the project copy, because a cloned repo can plant one. Project-scope installs are unsupported; install at global scope.

## Tests

```bash
python3 -I -m unittest discover -s tests
bash tests/test-capability-auto-install.sh
bash tests/test-versions.sh
bash tests/test-root-capability.sh   # needs gsd-core
```

## License

MIT, see [LICENSE](LICENSE).
