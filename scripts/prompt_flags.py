# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""UserPromptSubmit hook: a prompt containing `plainpy:skip` skips the finish check for that turn."""

from __future__ import annotations

from _common import project_dir, read_hook_input, read_session, save_session, session_id

SKIP_WORD = "plainpy:skip"


def main() -> None:
    hook_input = read_hook_input()
    project = project_dir(hook_input)
    session = session_id(hook_input)
    values = read_session(project, session)
    if "base" not in values:
        return
    skip = SKIP_WORD in hook_input.get("prompt", "")
    save_session(project, session, values | {"skip": skip, "blocks": 0, "last": None})
    if skip:
        print("plainpy: the finish check is skipped for this turn (plainpy:skip).")


if __name__ == "__main__":
    main()
