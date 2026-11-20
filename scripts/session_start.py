# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""SessionStart hook: snapshot the working tree as this session's base, record known test
failures, and add the plainpy doctrine to context.

The base is the working tree as it was when the session started, uncommitted and untracked
files included, so only what this session changes is judged."""

from __future__ import annotations

from pathlib import Path

from _common import (
    DOC_SUFFIXES,
    emit,
    is_python_project,
    load_settings,
    project_dir,
    prune_sessions,
    read_hook_input,
    read_json,
    read_session,
    save_session,
    session_id,
    snapshot,
)

DOCTRINE = """\
plainpy is active ({profile} profile). Python you write here is checked after every edit.
- Prefer plain functions. A class must hold state plus behaviour, or have more than one implementation.
- Use Pydantic only at I/O boundaries (HTTP, files, env, CLI). Inside, use functions, then dataclasses.
- No speculative generality: no unused parameters, flags, ABCs or extension points nobody asked for.
- Small means less structure, not less behaviour: implement everything the task asks, edge cases
  included. Use the stdlib before adding a dependency.
- Input from outside (files, CLI arguments, network) can be malformed in ways the task doesn't list:
  fail with the program's own error format, not a traceback.
- When a check fails, its message says how to fix it: make the code simpler, not just shorter.
- Suppress only a finding a check reported, naming the rule and a reason after ` -- `.
  Lint config changes and new runtime dependencies need the user's approval.
- When you finish, the whole change is checked: types, tests, dead code, class design,
  duplicates and new suppressions. Fix what it reports before finishing.
Only code you changed is judged; existing problems elsewhere are not your task unless asked."""


def known_test_failures(project: Path) -> list[str]:
    """Tests that failed in the last pytest run before this session, so they aren't blamed on it."""
    return sorted(read_json(project / ".pytest_cache" / "v" / "cache" / "lastfailed"))


def new_project(project: Path) -> bool:
    """Nothing but dotfiles, docs and folders (of data, say) yet, as in a fresh `git init`: what Claude writes here
    may well be Python. Another language's project has its own files at the top: a manifest or source."""
    try:
        files = [entry.name for entry in project.iterdir() if not entry.is_dir()]
        return all(name.startswith(".") or name.endswith(DOC_SUFFIXES) for name in files)
    except OSError:  # missing or unreadable: nothing to snapshot
        return False


def main() -> None:
    hook_input = read_hook_input()
    project = project_dir(hook_input)
    if not (is_python_project(project) or new_project(project)):
        return
    session = session_id(hook_input)
    if hook_input.get("source") in {"startup", "clear"} or "base" not in read_session(project, session):
        save_session(project, session, {"base": snapshot(project), "test_baseline": known_test_failures(project)})
        prune_sessions(project)
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
