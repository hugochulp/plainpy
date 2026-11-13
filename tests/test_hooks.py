"""End-to-end tests: run each hook script the way Claude Code does, on a temp git repo."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

SIMPLE = "def ok(x: int) -> int:\n    return x + 1\n"

COMPLEX = """
def tangled(x: int, y: int) -> int:
    if x:
        for i in range(y):
            if i:
                while x:
                    if x > 2:
                        x -= 1
                    elif x == 1:
                        if y:
                            return i
                    else:
                        break
    return x
"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True
    )


def run_hook(script: str, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    """Run a hook as Claude Code would; env comes from the monkeypatched os.environ."""
    return subprocess.run(
        ["uv", "run", "--quiet", "--script", str(SCRIPTS / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\n')
    (project / "app.py").write_text(SIMPLE + COMPLEX)
    git(project, "init", "-q", "-b", "main")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "init")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    return project


def start_session(repo: Path) -> subprocess.CompletedProcess[str]:
    return run_hook("session_start.py", {"source": "startup", "cwd": str(repo)})


def edit(path: Path) -> subprocess.CompletedProcess[str]:
    return run_hook("check_file.py", {"tool_name": "Edit", "tool_input": {"file_path": str(path)}})


def test_session_start_records_base_and_adds_doctrine(repo: Path) -> None:
    result = start_session(repo)
    output = json.loads(result.stdout)
    assert "plainpy is active (standard profile)" in output["hookSpecificOutput"]["additionalContext"]
    state_files = list(Path(os.environ["CLAUDE_PLUGIN_DATA"]).glob("projects/*/state.json"))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    assert json.loads(state_files[0].read_text())["base"] == head.stdout.strip()


def test_session_start_is_silent_outside_python_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "docs"
    other.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other))
    result = run_hook("session_start.py", {"source": "startup", "cwd": str(other)})
    assert result.returncode == 0
    assert result.stdout == ""


def test_new_complex_function_blocks(repo: Path) -> None:
    start_session(repo)
    new_file = repo / "fresh.py"
    new_file.write_text(COMPLEX.replace("tangled", "fresh"))
    result = edit(new_file)
    assert result.returncode == 2
    assert "CPX001" in result.stderr
    assert "fresh.py" in result.stderr


def test_existing_complexity_is_not_blamed_on_unrelated_edit(repo: Path) -> None:
    start_session(repo)
    app = repo / "app.py"
    app.write_text(app.read_text().replace("return x + 1", "return x + 2"))
    result = edit(app)
    assert result.returncode == 0, result.stderr


def test_worsening_existing_complexity_blocks(repo: Path) -> None:
    start_session(repo)
    app = repo / "app.py"
    worse = "                        x -= 1\n                        if y > x:\n                            y -= 1\n"
    app.write_text(app.read_text().replace("                        x -= 1\n", worse))
    result = edit(app)
    assert result.returncode == 2
    assert "tangled" in result.stderr


def test_lenient_profile_warns_without_blocking(repo: Path) -> None:
    (repo / ".plainpy.toml").write_text('profile = "lenient"\n\n[overrides]\ncognitive_max = 3\n')
    start_session(repo)
    new_file = repo / "fresh.py"
    new_file.write_text(COMPLEX.replace("tangled", "fresh"))
    result = edit(new_file)
    assert result.returncode == 0
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "CPX001" in context


def test_autofix_sorts_imports_but_keeps_unused_ones(repo: Path) -> None:
    start_session(repo)
    module = repo / "mod.py"
    module.write_text("import sys\nimport os\n\n\ndef cwd() -> str:\n    return os.getcwd()\n")
    result = edit(module)
    assert module.read_text().startswith("import os\nimport sys\n")
    assert "F401" not in result.stderr


def test_assert_is_allowed_in_tests(repo: Path) -> None:
    start_session(repo)
    test_file = repo / "tests" / "test_app.py"
    test_file.parent.mkdir()
    test_file.write_text("from app import ok\n\n\ndef test_ok():\n    assert ok(1) == 2\n")
    result = edit(test_file)
    assert result.returncode == 0, result.stderr


def test_non_python_files_are_ignored(repo: Path) -> None:
    notes = repo / "notes.md"
    notes.write_text("# notes\n")
    result = edit(notes)
    assert result.returncode == 0
    assert result.stdout == ""
