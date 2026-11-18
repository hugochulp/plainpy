# plainpy

A Claude Code plugin that keeps the Python Claude writes small and flat.
It does this with deterministic checks run as hooks, not with longer instructions.

**Status: v0.4.1.** Per-edit lint, complexity and anti-abstraction checks, guards against
silencing them, and a full check of the whole change when Claude finishes.

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
    `pyrefly: ignore`, `ty: ignore`, `pylint: disable`, `pragma: no cover` and
    `slopcheck: allow`;
  - a change to lint or type-checker config (`[tool.ruff]` and similar sections of
    `pyproject.toml`, `ruff.toml`, `.plainpy.toml`, `pyrightconfig.json`, ...) or a new
    runtime dependency (`[project.dependencies]`, `requirements*.txt`) **asks you** first.
- **Before every shell command:** `uv add`, `pip install`, `poetry add`, `pdm add`,
  `conda install` with new runtime packages, or a shell write to a config file, **asks you**
  first. Dev-only installs (`--dev`, `--group`) and `pip install -r` / `-e .` pass.
- **Headless runs** (`claude -p`, CI, the Agent SDK) have nobody to answer these questions, so
  Claude Code treats them as "no". Set `PLAINPY_ALLOW_DEPENDENCIES=1` in the environment to let
  new dependencies through; lint and type-checker config changes are still refused.
- **After every Write/Edit of a `.py` file:**
  1. applies safe Ruff fixes and formats the file;
  2. lints it with a broad Ruff rule set, checks cognitive complexity with complexipy, and
     runs slopcheck's single-file rules (see below);
  3. compares the findings with the same file when the session started. Only **new** problems,
     or size and complexity numbers that went **up**, are reported;
  4. reports them to Claude, which fixes them before moving on (`standard` and `strict`).
     In `lenient`, they are passed to Claude as warnings.

- **When Claude finishes (Stop):** checks everything changed since the session started, in
  parallel:

  | Check | Tool | Blocks? |
  |---|---|---|
  | Lint, complexity and single-file slopcheck rules on every changed file, unused imports included | Ruff, complexipy, slopcheck | yes |
  | New suppression comments anywhere in the diff, including any added through the shell | plainpy | yes |
  | Tests (runs only when `.venv/bin/pytest` exists) | pytest | yes |
  | Types (errors only; `strict` adds warnings) | basedpyright | yes |
  | `isinstance` and `None` checks the types already prove (SLP009) | basedpyright | in `strict`; otherwise reported |
  | Interfaces with one implementation, placeholder code, the class budget | slopcheck | yes |
  | Pydantic models away from I/O, unused options, tiny helpers used once, wide modules, too many exception classes | slopcheck | no, reported |
  | Duplicated code, deep inheritance, too many attributes | Pylint | yes |
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

### slopcheck

plainpy's own rules for over-abstraction (`scripts/slopcheck/`, stdlib `ast` only):
`edit_rules.py` runs after every edit, `finish_rules.py` when Claude finishes. Test files
and `.pyi` stubs are never judged, and only definitions that are new or changed in the session count.

| Rule | Flags | Runs | Default |
|---|---|---|---|
| SLP001 | An ABC with at most one implementation (subclasses at any depth count), or a Protocol that exactly one class in the project implements. Test fakes don't count | finish | error |
| SLP002 | A class with one public method (or `__call__`), or only static methods, and no base class: it should be a function | every edit | error |
| SLP004 | A function, or a method of a class without bases, that only passes its arguments, unchanged, to another call | every edit | error |
| SLP005 | A private helper under 3 statements that's used once | finish | warning |
| SLP003 | A Pydantic model that never reaches I/O: nothing validates it, builds it from a dict, dumps it, asks for its JSON schema or passes the class to a call (as LLM SDKs take `response_format=Model`), and no decorated handler takes it. Fields, bases and subclasses of a model that does count. `BaseSettings` and `SQLModel` are exempt; uses in tests don't count | finish | warning |
| SLP006 | More new classes in the session than the budget (lenient 6, standard 3, strict 2; `class_budget`). Exceptions don't count; a class moved to another file counts as new | finish | error |
| SLP007 | A parameter with a default that no call in the project passes, by name or position (calls in tests and through import aliases count). Skips private, decorated and special methods, methods a base class or subclass shares, and functions that are never called, passed around as values, or called with `*`/`**` | finish | warning |
| SLP008 | A new function whose body is only `pass`, `...`, a docstring or `raise NotImplementedError`, or that returns a constant while ignoring all its parameters. Skips decorated functions (`@abstractmethod`, `@overload`), special methods, methods a base class, subclass or project Protocol shares, and nested functions passed around as values (a callback that deliberately does nothing) | finish | error |
| SLP009 | basedpyright's `reportUnnecessaryIsInstance`, `reportUnnecessaryComparison` and `reportUnnecessaryContains`. Only reported when your basedpyright mode turns them on (its default and `strict` do; `standard` and `basic` don't). Being basedpyright's, it can't take a slopcheck allow comment | finish | warning (error in strict) |
| SLP010 | A module with more than 15 public functions and classes (reported again only when the count goes up; `__init__.py` is exempt), or a file named `utils.py`, `helpers.py` or `common.py` | finish | warning |
| SLP011 | More than 2 new exception classes in the session | finish | warning |

To keep one definition, put a reason on its `def` or `class` header (any line of it, so it
survives the formatter wrapping a long header):
`# slopcheck: allow SLP004 -- old name kept for existing callers`. The reason follows the
same rules as any other suppression.

Classes are told apart by their full name, module and nesting included (`pkg.a.Outer.Inner`),
so two classes that share a short name are never mixed up. A base class is followed through
imports: aliases, relative imports, and re-exports from a package's `__init__.py`. A base
that can't be followed, such as one from a star import, is matched by its short name, so a
rule can miss a finding there but never reports a wrong one.

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
class_budget = 4       # new classes per session before SLP006 blocks
```

| Setting | lenient | standard | strict |
|---|---|---|---|
| Findings block Claude | no (warn) | yes | yes |
| Cognitive complexity max (complexipy) | 25 | 15 | 10 |
| Cyclomatic complexity max (Ruff C901) | 15 | 10 | 8 |
| New classes per session (SLP006) | 6 | 3 | 2 |
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
