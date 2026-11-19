"""SlopCodeBench problem files: config, checkpoint specs as its runner renders them, and the hidden tests' needs."""

import re
import shutil
from functools import cache
from pathlib import Path

PROBLEMS = Path(__file__).resolve().parent / ".cache" / "scb-problems"  # gabeorlanski/scb-problems @ 9cd9ca3


@cache
def config(problem: str) -> str:
    return (PROBLEMS / problem / "config.yaml").read_text()


def entry_file(problem: str) -> str:
    return re.search(r"^entry_file: *(\S+)", config(problem), re.MULTILINE)[1]


def last_checkpoint(problem: str) -> int:
    return len(list((PROBLEMS / problem).glob("checkpoint_*.md")))


def test_packages(problem: str) -> list[str]:
    """SlopCodeBench's standard test packages (docs/evaluation/configuration.md) and the problem's own."""
    listed = re.search(r"^test_dependencies:\n((?: +- .+\n)+)", config(problem) + "\n", re.MULTILINE)
    own = re.findall(r"- *[\"']?([^\"'\s]+)", listed[1]) if listed else []
    return ["pytest", "pytest-timeout", "jsonschema", "deepdiff", *own]


def static_assets(problem: str) -> dict[str, str]:
    """Name to folder, e.g. {"sde_dir": "sde"}: copied into the workspace and to tests/assets/<name>
    (docs/execution/assets.md, docs/evaluation/configuration.md)."""
    listed = re.search(r"^static_assets:\n((?: +.+\n)+)", config(problem) + "\n", re.MULTILINE)
    if not listed:
        return {}
    assets = dict(re.findall(r"^  (\w+):\n    path: *(\S+)\n", listed[1], re.MULTILINE))
    if len(assets) != len(re.findall(r"^  \w+:", listed[1], re.MULTILINE)) or "save_path" in listed[1]:
        raise ValueError(f"{problem}: static_assets layout not handled:\n{listed[1]}")
    return assets


@cache
def tests_folder(problem: str) -> Path:
    """The problem's hidden tests folder, with the static assets its fixtures read."""
    tests = PROBLEMS / problem / "tests"
    for name, path in static_assets(problem).items():
        shutil.copytree(PROBLEMS / problem / path, tests / "assets" / name, dirs_exist_ok=True)
    return tests


def checkpoint_spec(problem: str, n: int) -> str:
    """A checkpoint's spec as SlopCodeBench's runner gives it (common/render.py): the canary comment at the top
    removed, and the entry-point placeholders filled in as its local Python environment does."""
    spec, entry = (PROBLEMS / problem / f"checkpoint_{n}.md").read_text(), entry_file(problem)
    spec = re.sub(r"^<!-- [\s\S]+? -->\n?", "", spec).replace("%%%ENTRYPOINT:entry_file%%%", f"{entry}.py")
    return spec.replace("%%%ENTRYPOINT:entry_command%%%", f"python {entry}.py").strip()
