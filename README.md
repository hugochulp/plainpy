# plainpy

A Claude Code plugin that keeps the Python Claude writes small and flat.
It does this with deterministic checks run as hooks, not with longer instructions.

**Status: v0.1.** Per-edit lint and complexity checks. Guards against suppressions,
the full check when Claude finishes, and the custom anti-abstraction rules come in later
milestones.

## What v0.1 does

- **Session start:** records the base revision (the merge-base with your default branch,
  else `HEAD`) and adds a short coding doctrine to Claude's context.
- **After every Write/Edit of a `.py` file:**
  1. applies safe Ruff fixes and formats the file;
  2. lints it with a broad Ruff rule set and checks cognitive complexity with complexipy;
  3. compares the findings with the same file at the base revision. Only **new** problems,
     or size and complexity numbers that went **up**, are reported;
  4. reports them to Claude, which fixes them before moving on (`standard` and `strict`).
     In `lenient`, they are passed to Claude as warnings.

Existing problems in code Claude didn't touch are never reported, so the plugin is safe
to use on existing repos.

## Install

Requirements: [`uv`](https://docs.astral.sh/uv/) and `git` on your `PATH`. Ruff and
complexipy are downloaded by `uvx` at the pinned versions in `tools.lock.toml` the first
time they run. If your project has them in `.venv`, those copies are used instead.

```
/plugin marketplace add hugochulp/plainpy
/plugin install plainpy@plainpy
```

To try a local checkout without installing it:

```bash
claude --plugin-dir /path/to/plainpy
```

## Configure

Optional `.plainpy.toml` at the project root:

```toml
profile = "standard"   # lenient | standard | strict

[overrides]            # optional; example values, not defaults
cognitive_max = 12
mccabe_max = 9
```

| Setting | lenient | standard | strict |
|---|---|---|---|
| Findings block Claude | no (warn) | yes | yes |
| Cognitive complexity max (complexipy) | 25 | 15 | 10 |
| Cyclomatic complexity max (Ruff C901) | 15 | 10 | 8 |

Ruff also enforces at most 5 arguments, 10 branches, 6 returns and 40 statements per
function. The plugin adds its rules on top of your own `[tool.ruff]` config with
`--extend-select`, so your excludes and per-file ignores still apply.

## Develop

```bash
uvx --python 3.13 --with pytest pytest -q
```

```bash
uvx ruff@0.17.5 check scripts tests && uvx ruff@0.17.5 format --check scripts tests
```

## License

MIT
