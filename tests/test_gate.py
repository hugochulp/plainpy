"""The finish check (Stop hook) on real temp repos: what blocks, what doesn't, and the loop guards."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import gate
import pytest
from _common import Settings
from _findings import Finding, ToolError

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

PYPROJECT = """\
[project]
name = "demo"
version = "0"
requires-python = ">=3.11"
dependencies = []

[tool.pytest.ini_options]
pythonpath = ["src"]
"""
CALC = "def add(a: int, b: int) -> int:\n    return a + b\n"
TEST = "from demo.calc import add\n\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n"
PROBLEMS = """

class Doubler:
    def __init__(self, factor: int) -> None:
        self.factor = factor

    def apply(self, v: int) -> int:
        return v * self.factor


def broken() -> int:
    return add(1, "2")
"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True
    )


def hook(script: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        ["uv", "run", "--quiet", "--script", str(SCRIPTS / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip().startswith("{") else {}


def stop(*, again: bool = False) -> dict[str, Any]:
    return hook("gate.py", {"session_id": "s1", "stop_hook_active": again})


@pytest.fixture(scope="session")
def shared_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    venv = tmp_path_factory.mktemp("venv") / ".venv"
    subprocess.run(["uv", "venv", "-q", "--python", "3.13", str(venv)], check=True)
    subprocess.run(["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"), "pytest"], check=True)
    return venv


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "proj"
    (project / "src" / "demo").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "pyproject.toml").write_text(PYPROJECT)
    (project / ".gitignore").write_text(".venv\n")
    (project / "src" / "demo" / "__init__.py").write_text("")
    (project / "src" / "demo" / "calc.py").write_text(CALC)
    (project / "tests" / "test_calc.py").write_text(TEST)
    git(project, "init", "-q", "-b", "main")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "base")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    return project


def start(repo: Path) -> None:
    hook("session_start.py", {"source": "startup", "cwd": str(repo), "session_id": "s1"})


def calc(repo: Path) -> Path:
    return repo / "src" / "demo" / "calc.py"


def touch_calc(repo: Path) -> None:
    """A harmless edit, so the check has a Python change to look at."""
    calc(repo).write_text(calc(repo).read_text().replace("return a + b", "return b + a"))


def test_clean_change_passes(repo: Path) -> None:
    start(repo)
    calc(repo).write_text(CALC + "\n\ndef sub(a: int, b: int) -> int:\n    return a - b\n")
    tests = TEST.replace("import add", "import add, sub") + "\n\ndef test_sub() -> None:\n    assert sub(3, 1) == 2\n"
    (repo / "tests" / "test_calc.py").write_text(tests)
    (repo / "src" / "demo" / "__init__.py").write_text("from demo.calc import add\n")  # a re-export, not F401
    assert "decision" not in stop()


def test_new_problems_block_with_one_section_each(repo: Path, shared_venv: Path) -> None:
    (repo / ".venv").symlink_to(shared_venv)
    start(repo)
    calc(repo).write_text("import sys\n\n\n" + CALC.replace("a + b", "a + b + 1") + PROBLEMS)
    (repo / "src" / "demo" / "extra.py").write_text("import os  # noqa\n")  # written as if through the shell
    outcome = stop()
    assert outcome["decision"] == "block"
    reason = outcome["reason"]
    for expected in (
        "tests/test_calc.py::test_add failed",
        "reportArgumentType",
        "R0903 Too few public methods",
        "unused-function",
        "'broken'",
        "`# noqa` needs a rule code and a reason",
        "F401",
    ):
        assert expected in reason, expected


def test_existing_problems_are_not_blamed(repo: Path) -> None:
    calc(repo).write_text(CALC + PROBLEMS)
    git(repo, "commit", "-qam", "problems already on main")
    start(repo)
    touch_calc(repo)
    assert "decision" not in stop()


def test_work_from_before_the_session_is_not_blamed(repo: Path) -> None:
    git(repo, "checkout", "-qb", "feat")
    calc(repo).write_text(CALC + PROBLEMS)
    git(repo, "commit", "-qam", "work in progress on a branch")
    (repo / "src" / "demo" / "wip.py").write_text("import os  # noqa\n")  # uncommitted, untracked
    start(repo)
    assert stop() == {}  # nothing changed in this session
    touch_calc(repo)
    assert "decision" not in stop()


def test_ignored_files_are_not_checked(repo: Path) -> None:
    (repo / ".gitignore").write_text(".venv\nscratch/\n")
    git(repo, "commit", "-qam", "ignore scratch")
    start(repo)
    (repo / "scratch").mkdir()
    (repo / "scratch" / "play.py").write_text(PROBLEMS.replace("add(1, ", "len("))
    touch_calc(repo)
    assert "decision" not in stop()


def test_renamed_file_keeps_its_old_problems_unblamed(repo: Path) -> None:
    (repo / "src" / "demo" / "legacy.py").write_text("import os  # noqa\n" + CALC + PROBLEMS)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "legacy module")
    start(repo)
    (repo / "src" / "demo" / "legacy.py").rename(repo / "src" / "demo" / "old.py")
    assert "decision" not in stop()


def test_unicode_file_names_are_checked(repo: Path) -> None:
    start(repo)
    (repo / "src" / "demo" / "café.py").write_text("import os  # noqa\n")
    reason = stop()["reason"]
    assert "café.py" in reason
    assert "`# noqa` needs a rule code and a reason" in reason


def test_data_classes_and_models_are_not_called_functions(repo: Path) -> None:
    start(repo)
    (repo / "src" / "demo" / "models.py").write_text(
        "from dataclasses import dataclass\n\nfrom pydantic import BaseModel\n\n\n"
        "class User(BaseModel):\n    name: str\n    email: str\n\n\n"
        "@dataclass\nclass Point:\n    x: int\n    y: int\n"
    )
    (repo / "tests" / "test_models.py").write_text(
        "from demo.models import Point, User\n\n\ndef test_models() -> None:\n"
        '    assert User(name="a", email="b").name == "a"\n    assert Point(1, 2) == Point(1, 2)\n'
    )
    assert "decision" not in stop()


def test_awkward_files_do_not_crash_the_check(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "shared").mkdir()
    (repo / "shared").symlink_to(tmp_path / "shared")  # a tracked link pointing outside the repo
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "link")
    start(repo)
    (tmp_path / "elsewhere.py").write_text("VALUE = 1\n")
    (repo / "src" / "demo" / "linked.py").symlink_to(tmp_path / "elsewhere.py")  # a .py link out of the repo
    (repo / "src" / "demo" / "legacy_text.py").write_bytes(b"# -*- coding: latin-1 -*-\nNAME = '\xe9'\n")
    unreachable = "def first() -> int:\n    return 1\n    print('x')\n"
    (repo / "src" / "demo" / "dead.py").write_text(unreachable + "\n\n" + unreachable.replace("first", "second"))
    reason = stop()["reason"]
    assert "unreachable-code x2" in reason
    assert "after 'return'" in reason


def test_duplicates_already_in_the_base_are_not_blamed(repo: Path) -> None:
    body = "".join(f"    total += {n} * value\n" for n in range(8))
    duplicate = f"def {{name}}(value: int) -> int:\n    total = 0\n{body}    return total\n"
    for name in ("b_mod", "c_mod"):
        (repo / "src" / "demo" / f"{name}.py").write_text(duplicate.format(name=f"{name}_sum"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "duplicated modules")
    start(repo)
    (repo / "src" / "demo" / "z_new.py").write_text("LIMIT = 3\n")  # pylint now reports on another file
    (repo / "src" / "demo" / "c_mod.py").rename(repo / "src" / "demo" / "a_mod.py")  # and module names change
    assert "decision" not in stop()


def test_a_class_that_grows_is_reported(repo: Path) -> None:
    fields = "".join(f"        self.f{n} = {n}\n" for n in range(8))
    big = (
        f"class Big:\n    def __init__(self) -> None:\n{fields}\n    def total(self) -> int:\n        return self.f0\n"
    )
    (repo / "src" / "demo" / "big.py").write_text(big)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "a class with 8 attributes")
    start(repo)
    grown = big.replace("        self.f7 = 7\n", "        self.f7 = 7\n        self.f8 = 8\n")
    (repo / "src" / "demo" / "big.py").write_text(grown)
    reason = stop()["reason"]
    assert "R0902 Too many instance attributes (9/7)" in reason
    assert "R0903" not in reason  # unchanged since the base


def test_known_failing_tests_are_not_blamed(repo: Path, shared_venv: Path) -> None:
    (repo / ".venv").symlink_to(shared_venv)
    calc(repo).write_text(CALC.replace("a + b", "a - b"))
    (repo / "tests" / "test_spaces.py").write_text(
        'import pytest\n\n\n@pytest.mark.parametrize("text", ["a b"])\n'
        'def test_text(text: str) -> None:\n    assert text == "x"\n'
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "break things on main")
    cache = repo / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True)
    known = {"tests/test_calc.py::test_add": True, "tests/test_spaces.py::test_text[a b]": True}
    (cache / "lastfailed").write_text(json.dumps(known))
    start(repo)
    calc(repo).write_text(calc(repo).read_text().replace("return a - b", "return -b + a"))
    (repo / "tests" / "test_spaces.py").rename(repo / "tests" / "test_text.py")  # known failure, new file name
    assert "decision" not in stop()


def test_pytest_without_a_summary_is_not_a_pass(repo: Path, shared_venv: Path) -> None:
    (repo / ".venv").symlink_to(shared_venv)
    (repo / "pyproject.toml").write_text(PYPROJECT + 'addopts = "--no-summary"\n')
    git(repo, "commit", "-qam", "no summary")
    start(repo)
    calc(repo).write_text(CALC.replace("a + b", "a - b"))
    outcome = stop()
    assert "decision" not in outcome
    assert "listed no failures" in outcome["systemMessage"]


def test_gives_up_when_the_same_problems_come_back(repo: Path) -> None:
    start(repo)
    calc(repo).write_text(CALC + PROBLEMS)
    assert stop()["decision"] == "block"
    touch_calc(repo)
    outcome = stop(again=True)
    assert "decision" not in outcome
    assert "came back unchanged" in outcome["systemMessage"]


def test_gives_up_at_once_when_nothing_changed_after_a_block(repo: Path) -> None:
    start(repo)
    calc(repo).write_text(CALC + PROBLEMS)
    assert stop()["decision"] == "block"
    outcome = stop(again=True)
    assert "decision" not in outcome
    assert "changed nothing since it asked" in outcome["systemMessage"]
    assert "R0903" in outcome["systemMessage"]
    assert stop() == {}  # the user has seen it; the next turn doesn't re-run the same check


def test_gives_up_after_max_blocks(repo: Path) -> None:
    (repo / ".plainpy.toml").write_text("[overrides]\nmax_gate_blocks = 1\n")
    start(repo)
    calc(repo).write_text(CALC + PROBLEMS)
    assert stop()["decision"] == "block"
    calc(repo).write_text(CALC + PROBLEMS.replace("broken", "still_broken"))
    outcome = stop(again=True)
    assert "decision" not in outcome
    assert "already asked 1 times" in outcome["systemMessage"]


def test_skip_word_skips_the_turn(repo: Path) -> None:
    start(repo)
    calc(repo).write_text(CALC + PROBLEMS)
    hook("prompt_flags.py", {"session_id": "s1", "prompt": "quick spike, plainpy:skip"})
    assert stop() == {}
    hook("prompt_flags.py", {"session_id": "s1", "prompt": "now tidy it up"})
    assert stop()["decision"] == "block"


def test_lenient_profile_reports_without_blocking(repo: Path) -> None:
    (repo / ".plainpy.toml").write_text('profile = "lenient"\n')
    start(repo)
    calc(repo).write_text(CALC + PROBLEMS)
    outcome = stop()
    assert "decision" not in outcome
    assert "not blocking" in outcome["systemMessage"]


def test_unchanged_files_after_a_clean_run_skip_the_check(repo: Path) -> None:
    start(repo)
    calc(repo).write_text(CALC + "\n\ndef double(a: int) -> int:\n    return add(a, a)\n")
    tests = (
        TEST.replace("import add", "import add, double") + "\n\ndef test_double() -> None:\n    assert double(2) == 4\n"
    )
    (repo / "tests" / "test_calc.py").write_text(tests)
    first = stop()
    assert "decision" not in first
    assert stop() == {}


def test_unresolved_imports_without_a_venv_do_not_block(repo: Path) -> None:
    start(repo)
    (repo / "src" / "demo" / "http.py").write_text(
        "import requests\n\n\ndef client() -> object:\n    return requests\n"
    )
    (repo / "tests" / "test_http.py").write_text(
        "from demo.http import client\n\n\ndef test_client() -> None:\n    assert client() is not None\n"
    )
    outcome = stop()
    assert "decision" not in outcome
    assert "DEP001 'requests' imported but missing" in outcome["systemMessage"]


def test_a_tool_that_fails_is_reported_and_checked_again(repo: Path) -> None:
    broken = repo / ".venv" / "bin" / "basedpyright"
    broken.parent.mkdir(parents=True)
    broken.write_text("#!/bin/sh\necho 'no network' >&2\nexit 3\n")
    broken.chmod(0o755)
    start(repo)
    touch_calc(repo)
    first = stop()
    assert "decision" not in first
    assert "basedpyright failed: no network" in first["systemMessage"]
    assert "basedpyright failed" in stop()["systemMessage"]  # not marked clean, so it runs again


def test_an_incomplete_block_is_checked_again(repo: Path) -> None:
    broken = repo / ".venv" / "bin" / "basedpyright"
    broken.parent.mkdir(parents=True)
    broken.write_text("#!/bin/sh\nexit 3\n")
    broken.chmod(0o755)
    start(repo)
    calc(repo).write_text(CALC + PROBLEMS)
    assert stop()["decision"] == "block"
    assert "changed nothing since it asked" in stop(again=True)["systemMessage"]
    assert stop()["decision"] == "block"  # basedpyright never ran, so the next turn checks again


def test_each_session_keeps_its_own_base(repo: Path) -> None:
    start(repo)
    calc(repo).write_text(CALC + PROBLEMS)
    hook("session_start.py", {"source": "startup", "cwd": str(repo), "session_id": "s2"})
    assert hook("gate.py", {"session_id": "s2", "stop_hook_active": False}) == {}  # s2 started after the edit
    assert stop()["decision"] == "block"  # s1 still judges it


def test_a_base_that_cannot_be_checked_never_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pre_existing = Finding("unused-function", 3, "'old'", "", "'old'", file="m.py")
    check = gate.TreeCheck("Dead code (vulture)", "vulture", lambda *_: [pre_existing])
    monkeypatch.setattr(gate, "TREE_CHECKS", {"dead code": check})
    monkeypatch.setattr(gate, "python_files_in", lambda *_: ["m.py"])
    monkeypatch.setattr(gate, "base_findings", lambda *_: {"dead code": ToolError("vulture timed out after 150 s")})
    settings = Settings(profile="standard", blocking=True, cognitive_max=15, mccabe_max=10)
    change = gate.Change(tmp_path, tmp_path, ".", "b", "c", settings, ["m.py"], {}, frozenset())
    [section] = gate.tree_sections(change, tmp_path)
    assert section.findings == []
    assert not section.blocking
    assert section.incomplete
    assert "vulture timed out" in section.notes[0]
