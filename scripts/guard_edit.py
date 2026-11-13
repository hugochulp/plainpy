# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""PreToolUse hook on Write/Edit/MultiEdit: stop Claude silencing checks.

- New suppression comments in Python files are denied unless they name the
  exact rule and give a reason: `# noqa: C901 -- parser mirrors the spec grammar`.
  File-wide suppressions are always denied.
- Edits to lint/type-checker config, and new runtime dependencies, are put to
  the user as a permission prompt.

Everything is judged on the file as it would be after the edit, compared with
the file as it is now, so existing suppressions and config never trigger.
"""

from __future__ import annotations

import io
import re
import tokenize
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from _common import CONFIG_FILES, pre_tool_decision, read_hook_input

FILE_WIDE = re.compile(
    r"#\s*(ruff:\s*noqa|flake8:\s*noqa|mypy:\s*ignore-errors|pylint:\s*skip-file|pyright:\s*basic)", re.IGNORECASE
)
LINE_LEVEL = re.compile(
    r"#\s*(?P<kind>noqa|type:\s*ignore|pyright:\s*ignore|pyrefly:\s*ignore|ty:\s*ignore"
    r"|pylint:\s*disable|pragma:\s*no\s*cover)"
    r"(?P<codes>:\s*[\w-]+(?:\s*,\s*[\w-]+)*|\[[^\]]+\]|=[\w,-]+)?",
    re.IGNORECASE,
)
REASON = re.compile(r"\s--\s+\S+(?:\s+\S+){2,}")
UNCODED_KINDS = ("pragma",)

CONFIG_SECTIONS = (
    "ruff", "basedpyright", "pyright", "complexipy", "mypy", "pylint",
    "vulture", "deptry", "tach", "pyrefly", "ty", "coverage", "plainpy",
)  # fmt: skip
REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

SUPPRESSION_HELP = (
    "Fix the code instead. If a suppression is truly right, name the exact rule and give a reason "
    "of 3+ words after ` -- `, e.g. `# noqa: C901 -- parser mirrors the spec grammar`."
)


def after_edit(tool_input: dict[str, Any], before: str) -> str | None:
    """The file's content once this Write/Edit/MultiEdit applies; None if it would fail."""
    if "content" in tool_input:
        return str(tool_input["content"])
    text = before
    for edit in tool_input.get("edits") or [tool_input]:
        old, new = edit.get("old_string", ""), edit.get("new_string", "")
        if old not in text:
            return None
        text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
    return text


def comments(source: str) -> list[str]:
    """Comment text only, so a `# noqa` inside a string literal is not a suppression."""
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        return [token.string for token in tokens if token.type == tokenize.COMMENT]
    except (tokenize.TokenError, SyntaxError):
        return [line[line.index("#") :] for line in source.splitlines() if "#" in line]


def suppression_problem(comment: str) -> str | None:
    if FILE_WIDE.search(comment):
        return f"File-wide suppression `{comment.strip()}` is never allowed. Fix the code instead."
    found = list(LINE_LEVEL.finditer(comment))
    if not found:
        return None
    uncoded = [m for m in found if not m.group("codes") and not m.group("kind").lower().startswith(UNCODED_KINDS)]
    if uncoded or not REASON.search(comment):
        return f"Suppression `{comment.strip()}` needs a rule code and a reason. {SUPPRESSION_HELP}"
    return None


def added_suppressions(before: str, after: str) -> list[str]:
    """Suppression comments in `after` that were not in `before`."""
    added = Counter(comments(after)) - Counter(comments(before))
    return [comment for comment in added.elements() if FILE_WIDE.search(comment) or LINE_LEVEL.search(comment)]


def suppression_problems(before: str, after: str) -> list[str]:
    return [problem for comment in added_suppressions(before, after) if (problem := suppression_problem(comment))]


def parse_toml(text: str) -> dict[str, Any] | None:
    """The parsed document ({} for an empty one), or None if it isn't valid TOML."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def requirement_names(lines: list[str]) -> set[str]:
    names = (REQUIREMENT_NAME.match(line) for line in lines if not line.lstrip().startswith(("#", "-")))
    return {match.group(1).lower() for match in names if match}


def pyproject_changes(before: str, after: str) -> tuple[list[str], set[str]]:
    """(changed tool sections, newly added runtime dependencies)."""
    old, new = parse_toml(before) or {}, parse_toml(after)
    old_tool = old.get("tool", {})
    if new is None:
        # The edit can't be compared, so every protected section it had may be at stake.
        return [name for name in CONFIG_SECTIONS if name in old_tool], set()
    new_tool = new.get("tool", {})
    changed = [name for name in CONFIG_SECTIONS if old_tool.get(name) != new_tool.get(name)]
    old_deps = requirement_names(old.get("project", {}).get("dependencies", []))
    new_deps = requirement_names(new.get("project", {}).get("dependencies", []))
    return changed, new_deps - old_deps


def config_question(path: Path, before: str, after: str) -> str | None:
    """A question for the user if this edit changes lint config or adds a dependency."""
    name = path.name
    if name in CONFIG_FILES:
        return f"Claude wants to change {name}, which configures plainpy's checks. Allow it?"
    if name.startswith("requirements") and name.endswith((".txt", ".in")):
        added = requirement_names(after.splitlines()) - requirement_names(before.splitlines())
        return f"Claude wants to add dependencies: {', '.join(sorted(added))}. Allow it?" if added else None
    if name != "pyproject.toml":
        return None
    sections, deps = pyproject_changes(before, after)
    parts = [f"change [tool.{s}] config" for s in sections] + (
        [f"add dependencies: {', '.join(sorted(deps))}"] if deps else []
    )
    return f"Claude wants to {'; '.join(parts)} in pyproject.toml. Allow it?" if parts else None


def main() -> None:
    tool_input = read_hook_input().get("tool_input", {})
    file_path = tool_input.get("file_path")
    if not file_path:
        return
    path = Path(file_path)
    before = path.read_text(errors="replace") if path.is_file() else ""
    after = after_edit(tool_input, before)
    if after is None:
        return
    problems = suppression_problems(before, after) if path.suffix in {".py", ".pyi"} else []
    if problems:
        pre_tool_decision("deny", "plainpy: " + " ".join(problems))
        return
    question = config_question(path, before, after)
    if question:
        pre_tool_decision("ask", f"plainpy: {question}")


if __name__ == "__main__":
    main()
