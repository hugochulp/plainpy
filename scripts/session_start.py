# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""SessionStart hook: snapshot the working tree as this session's base, record known test
failures, and add the plainpy doctrine to context.

The base is the working tree as it was when the session started, uncommitted and untracked
files included, so only what this session changes is judged."""

from __future__ import annotations

import json
from pathlib import Path

from _common import (
    emit,
    is_python_project,
    load_settings,
    project_dir,
    read_hook_input,
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
- Keep the diff small. Use the stdlib before adding a dependency.
- When a check fails, flatten the logic (early returns, guard clauses, a dict lookup, fewer cases).
  Never split code into single-use helpers, wrap it in new classes, or add noqa/type: ignore to pass.
- A suppression comment is refused unless it names the rule and gives a reason after ` -- `.
  Lint config changes and new runtime dependencies need the user's approval.
- When you finish, the whole change is checked: types, tests, dead code, class design,
  duplicates and new suppressions. Fix what it reports before finishing.
Only code you changed is judged; existing problems elsewhere are not your task unless asked."""


def known_test_failures(project: Path) -> list[str]:
    """Tests that failed in the last pytest run before this session, so they aren't blamed on it."""
    last_failed = project / ".pytest_cache" / "v" / "cache" / "lastfailed"
    try:
        return sorted(json.loads(last_failed.read_text()))
    except (OSError, ValueError):
        return []


def main() -> None:
    hook_input = read_hook_input()
    project = project_dir(hook_input)
    if not is_python_project(project):
        return
    session = session_id(hook_input)
    if hook_input.get("source") in {"startup", "clear"} or "base" not in read_session(project, session):
        save_session(project, session, {"base": snapshot(project), "test_baseline": known_test_failures(project)})
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
