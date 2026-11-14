# plainpy

A Claude Code plugin that keeps the Python Claude writes small and flat.
It does this with deterministic checks run as hooks, not with longer instructions.

**Status: v0.3.** Per-edit lint and complexity checks, guards against silencing them, and a
full check of the whole change when Claude finishes. Custom anti-abstraction rules come in
a later milestone.

## What it does

- **Session start:** takes a snapshot of your working tree as this session's base,
  uncommitted and untracked files included, and adds a short coding doctrine to Claude's
  context. The snapshot is a git tree object written through a scratch index: your staging
  area, branches and history are not touched.
- **Before every Write/Edit:**
  - a new suppression comment in a `.py` file is **refused** unless it names the exact rule
    and gives a reason: `# noqa: C901 -- parser mirrors the spec grammar`. File-wide
    suppressions (`# ruff: noqa`, `# mypy: ignore-errors`, `# pylint: skip-file`, ...) are
    always refused. Covers `noqa` (including complexipy's), `type: ignore`, `pyright: ignore`,
    `pyrefly: ignore`, `ty: ignore`, `pylint: disable` and `pragma: no cover`;
  - a change to lint or type-checker config (`[tool.ruff]` and similar sections of
    `pyproject.toml`, `ruff.toml`, `.plainpy.toml`, `pyrightconfig.json`, ...) or a new
    runtime dependency (`[project.dependencies]`, `requirements*.txt`) **asks you** first.
- **Before every shell command:** `uv add`, `pip install`, `poetry add`, `pdm add`,
  `conda install` with new runtime packages, or a shell write to a config file, **asks you**
  first. Dev-only installs (`--dev`, `--group`) and `pip install -r` / `-e .` pass.
- **After every Write/Edit of a `.py` file:**
  1. applies safe Ruff fixes and formats the file;
  2. lints it with a broad Ruff rule set and checks cognitive complexity with complexipy;
  3. compares the findings with the same file when the session started. Only **new** problems,
     or size and complexity numbers that went **up**, are reported;
  4. reports them to Claude, which fixes them before moving on (`standard` and `strict`).
     In `lenient`, they are passed to Claude as warnings.

- **When Claude finishes (Stop):** checks everything changed since the session started, in
  parallel:

  | Check | Tool | Blocks? |
  |---|---|---|
  | Lint and complexity on every changed file, unused imports included | Ruff, complexipy | yes |
  | New suppression comments anywhere in the diff, including any added through the shell | plainpy | yes |
  | Tests (runs only when `.venv/bin/pytest` exists) | pytest | yes |
  | Types (errors only; `strict` adds warnings) | basedpyright | yes |
  | One-method classes (with no base class), duplicated code, deep inheritance, too many attributes | Pylint | yes |
  | Unused functions, classes, methods and unreachable code | vulture | yes |
  | Unused, missing and transitive dependencies | deptry | no, reported |
  | Suppressions added with a reason | plainpy | no, listed for your review |

  New problems go back to Claude, which keeps working until they're fixed. Claude stops
  being asked when the same problems come back unchanged, when it stops again without
  changing anything, or after 3 attempts in one turn (`max_gate_blocks`), and you get a
  summary of what's left. Put `plainpy:skip` anywhere in a prompt to skip the check for that
  turn. Nothing is re-run when files haven't changed since the last complete check, or when
  only Markdown or reStructuredText files changed. If a tool fails to run, you get a note
  and the check runs again next time.

Only what changed during the session is judged: problems already in the code, in earlier
commits on your branch, or in uncommitted work from before the session are never reported,
so the plugin is safe to use on existing repos. Gitignored files are never checked, renamed
files keep their old problems unblamed, and base results are cached per snapshot. Tests
that already failed in your last pytest run before the session (`.pytest_cache`) aren't
blamed on Claude. The finish check needs a git repo. Without a project `.venv`, unresolved
third-party imports aren't treated as type errors.

Known limits: pulling new commits during a session (e.g. `git pull` on `main`) makes them
part of the session's change; start a new session (or `/clear`) afterwards. Untracked files
over 1 MB, and folders like `.venv`, `venv`, `build` and `node_modules`, are left out of
snapshots.

Trust: the finish check runs your project's own tools when they're in `.venv/bin`
(including `pytest`, which runs your tests), and Pylint reads your project's Pylint config.
Use plainpy in folders you trust, as you would Claude Code itself.

If vulture flags something that's used dynamically (a framework decorator, an entry point),
list it in `vulture_whitelist.py` at the project root. Claude has to ask you before editing
that file.

## Install

Requirements: [`uv`](https://docs.astral.sh/uv/) and `git` on your `PATH`. The tools are
downloaded by `uvx` at the pinned versions in `tools.lock.toml` the first time they run,
so the first finish check in a new environment takes longer. If your project has a tool in
`.venv`, that copy is used instead.

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
max_gate_blocks = 3    # finish-check attempts per turn before plainpy stops asking
```

| Setting | lenient | standard | strict |
|---|---|---|---|
| Findings block Claude | no (warn) | yes | yes |
| Cognitive complexity max (complexipy) | 25 | 15 | 10 |
| Cyclomatic complexity max (Ruff C901) | 15 | 10 | 8 |
| Type warnings block (not just errors) | no | no | yes |

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
