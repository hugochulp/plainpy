"""Attempts to silence or route around plainpy's checks, and the edits that must still pass."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

CODE = "def parse(text: str) -> int:\n    return int(text)\n"
PYPROJECT = """\
[project]
name = "demo"
version = "0"
dependencies = ["httpx>=0.27"]

[tool.ruff.lint]
select = ["E", "F"]
"""


def decision(script: str, tool_input: dict[str, Any]) -> str | None:
    """The hook's permissionDecision, or None when it stays silent."""
    result = subprocess.run(
        ["uv", "run", "--quiet", "--script", str(SCRIPTS / script)],
        input=json.dumps({"tool_input": tool_input}),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] if result.stdout else None


def edit(path: Path, old: str, new: str) -> str | None:
    return decision("guard_edit.py", {"file_path": str(path), "old_string": old, "new_string": new})


@pytest.fixture
def module(tmp_path: Path) -> Path:
    path = tmp_path / "parse.py"
    path.write_text(CODE)
    return path


@pytest.fixture
def pyproject(tmp_path: Path) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(PYPROJECT)
    return path


@pytest.mark.parametrize(
    "comment",
    [
        "# noqa",
        "# noqa: C901",
        "# noqa: complexipy",
        "# type: ignore",
        "# type: ignore[arg-type]",
        "# pyright: ignore",
        "# pragma: no cover",
        "# pylint: disable=too-many-branches",
        "# noqa -- this one has a reason but no rule code",
        "# noqa: E501 -- too short",
    ],
)
def test_unexplained_suppressions_are_denied(module: Path, comment: str) -> None:
    assert edit(module, "return int(text)", f"return int(text)  {comment}") == "deny"


@pytest.mark.parametrize(
    "comment",
    [
        "# ruff: noqa",
        "# ruff: noqa: E501 -- whole file is generated code",
        "# mypy: ignore-errors",
        "# pylint: skip-file",
    ],
)
def test_file_wide_suppressions_are_always_denied(module: Path, comment: str) -> None:
    assert edit(module, "def parse", f"{comment}\ndef parse") == "deny"


@pytest.mark.parametrize(
    "comment",
    [
        "# noqa: C901 -- parser mirrors the spec grammar",
        "# type: ignore[no-untyped-call] -- vendor SDK ships without stubs",
        "# pragma: no cover -- only reachable on Windows hosts",
    ],
)
def test_explained_suppressions_pass(module: Path, comment: str) -> None:
    assert edit(module, "return int(text)", f"return int(text)  {comment}") is None


def test_suppression_text_inside_a_string_passes(module: Path) -> None:
    assert edit(module, "return int(text)", 'return int(text.removesuffix("# noqa"))') is None


def test_existing_suppressions_are_not_blamed(tmp_path: Path) -> None:
    path = tmp_path / "old.py"
    path.write_text("import os  # noqa\n\n\ndef parse(text: str) -> int:\n    return int(text)\n")
    assert edit(path, "return int(text)", "return int(text.strip())") is None


def test_multiedit_and_write_are_checked(module: Path, tmp_path: Path) -> None:
    edits = [{"old_string": "int(text)", "new_string": "int(text)  # type: ignore"}]
    assert decision("guard_edit.py", {"file_path": str(module), "edits": edits}) == "deny"
    new_file = {"file_path": str(tmp_path / "new.py"), "content": "x = 1  # noqa\n"}
    assert decision("guard_edit.py", new_file) == "deny"


def test_lint_config_changes_ask_the_user(pyproject: Path, tmp_path: Path) -> None:
    assert edit(pyproject, 'select = ["E", "F"]', 'select = ["E"]') == "ask"
    nested_ruff = {"file_path": str(tmp_path / "pkg" / "ruff.toml"), "content": "[lint]\nignore = ['C901']\n"}
    assert decision("guard_edit.py", nested_ruff) == "ask"
    (tmp_path / ".plainpy.toml").write_text('profile = "standard"\n')
    assert edit(tmp_path / ".plainpy.toml", "standard", "lenient") == "ask"


def test_new_runtime_dependencies_ask_the_user(pyproject: Path, tmp_path: Path) -> None:
    assert edit(pyproject, '["httpx>=0.27"]', '["httpx>=0.27", "requests"]') == "ask"
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("httpx\n")
    assert edit(requirements, "httpx\n", "httpx\nrich==13.0\n") == "ask"


@pytest.mark.parametrize("content", ["", "# emptied\n", "[project\nname = 'broken'\n"])
def test_wiping_or_breaking_pyproject_config_asks_the_user(pyproject: Path, content: str) -> None:
    assert decision("guard_edit.py", {"file_path": str(pyproject), "content": content}) == "ask"


def test_rewriting_a_pyproject_without_tool_config_passes(tmp_path: Path) -> None:
    plain = tmp_path / "pyproject.toml"
    plain.write_text('[project]\nname = "demo"\n')
    assert decision("guard_edit.py", {"file_path": str(plain), "content": "# emptied\n"}) is None


def test_unrelated_pyproject_and_file_edits_pass(pyproject: Path, tmp_path: Path) -> None:
    assert edit(pyproject, 'version = "0"', 'version = "1"') is None
    assert edit(pyproject, '["httpx>=0.27"]', '["httpx>=0.28"]') is None
    notes = tmp_path / "notes.md"
    notes.write_text("# noqa is a lint pragma\n")
    assert edit(notes, "lint", "Ruff") is None


@pytest.mark.parametrize(
    "command",
    [
        "uv add requests",
        "cd app && uv add 'httpx>=0.27'",
        "python -m pip install rich",
        "uv pip install pydantic",
        "poetry add arrow",
        "echo 'ignore = [\"C901\"]' >> ruff.toml",
        "sed -i '' 's/max-complexity = 10/max-complexity = 30/' pyproject.toml",
        "rm .plainpy.toml",
        "rm .pylintrc",
        "echo 'ignore = [\"C901\"]' >> .ruff.toml",
        "cp other.ini .mypy.ini",
        "uv add --dev pytest\nuv add requests",
        "cat README.md\nsed -i s/10/30/ pyproject.toml",
        "ls;\n\nuv add rich",
        "cat > ruff.toml <<'EOF'\n[lint]\nignore = ['C901']\nEOF",
        "uv add requests 2>&1 | tail -3",
    ],
)
def test_shell_dependency_and_config_changes_ask_the_user(command: str) -> None:
    assert decision("guard_bash.py", {"command": command}) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "uv add --dev pytest",
        "uv add --group lint ruff",
        "pip install -r requirements.txt",
        "uv pip install -e .",
        "cat pyproject.toml",
        "grep -n ruff pyproject.toml 2>/dev/null",
        "uv run pytest -q > out.txt",
        "ls -la",
        "grep '> pyproject.toml' notes.md",
        "echo 'see >> ruff.toml in the docs'",
        "uv add --dev pytest\nuv add --dev ruff",
        "git commit -m 'Raise limit\n\nuv add requests is not run here'",
    ],
)
def test_ordinary_shell_commands_pass(command: str) -> None:
    assert decision("guard_bash.py", {"command": command}) is None
