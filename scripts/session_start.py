# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""SessionStart hook: record the base revision and add the plainpy doctrine to context."""

from __future__ import annotations

from pathlib import Path

from _common import (
    emit,
    git,
    is_python_project,
    load_settings,
    project_dir,
    read_hook_input,
    read_state,
    write_state,
)

DOCTRINE = """\
plainpy is active ({profile} profile). Python you write here is checked after every edit.
- Prefer plain functions. A class must hold state plus behaviour, or have more than one implementation.
- Use Pydantic only at I/O boundaries (HTTP, files, env, CLI). Inside, use functions, then dataclasses.
- No speculative generality: no unused parameters, flags, ABCs or extension points nobody asked for.
- Keep the diff small. Use the stdlib before adding a dependency.
- When a check fails, flatten the logic (early returns, guard clauses, a dict lookup, fewer cases).
  Never split code into single-use helpers, wrap it in new classes, or add noqa/type: ignore to pass.
Only code you changed is judged; existing problems elsewhere are not your task unless asked."""


def default_branch(project: Path) -> str | None:
    remote_head = git(project, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if remote_head:
        return remote_head
    for name in ("main", "master"):
        if git(project, "rev-parse", "--verify", "--quiet", name):
            return name
    return None


def find_base_revision(project: Path) -> str | None:
    head = git(project, "rev-parse", "--verify", "--quiet", "HEAD")
    if head is None:
        return None
    branch = default_branch(project)
    merge_base = git(project, "merge-base", "HEAD", branch) if branch else None
    return merge_base or head


def main() -> None:
    hook_input = read_hook_input()
    project = project_dir(hook_input)
    if not is_python_project(project):
        return
    state = read_state(project)
    if hook_input.get("source") in {"startup", "clear"} or "base" not in state:
        state["base"] = find_base_revision(project)
        write_state(project, state)
    settings = load_settings(project)
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": DOCTRINE.format(profile=settings.profile),
            }
        }
    )


if __name__ == "__main__":
    main()
