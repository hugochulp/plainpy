"""Shared helpers for plainpy hook scripts: hook I/O, settings, state, git, tools."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

PROFILES: dict[str, dict[str, Any]] = {
    "lenient": {"blocking": False, "cognitive_max": 25, "mccabe_max": 15},
    "standard": {"blocking": True, "cognitive_max": 15, "mccabe_max": 10},
    "strict": {"blocking": True, "cognitive_max": 10, "mccabe_max": 8},
}


@dataclass(frozen=True)
class Settings:
    profile: str
    blocking: bool
    cognitive_max: int
    mccabe_max: int


def read_hook_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


def project_dir(hook_input: dict[str, Any]) -> Path:
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env_dir or hook_input.get("cwd") or Path.cwd()).resolve()


def is_python_project(project: Path) -> bool:
    markers = ("pyproject.toml", "setup.py", "setup.cfg")
    if any((project / name).exists() for name in markers):
        return True
    return any(project.glob("*.py")) or any(project.glob("src/**/*.py"))


def load_settings(project: Path) -> Settings:
    config_path = project / ".plainpy.toml"
    config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    profile = config.get("profile", "standard")
    if profile not in PROFILES:
        profile = "standard"
    values = PROFILES[profile] | config.get("overrides", {})
    return Settings(
        profile=profile,
        blocking=bool(values["blocking"]),
        cognitive_max=int(values["cognitive_max"]),
        mccabe_max=int(values["mccabe_max"]),
    )


def state_path(project: Path) -> Path:
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA")
    root = Path(data_dir) if data_dir else Path.home() / ".cache" / "plainpy"
    digest = hashlib.sha256(str(project).encode()).hexdigest()[:16]
    return root / "projects" / digest / "state.json"


def read_state(project: Path) -> dict[str, Any]:
    path = state_path(project)
    return json.loads(path.read_text()) if path.exists() else {}


def write_state(project: Path, state: dict[str, Any]) -> None:
    path = state_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def git_raw(cwd: Path, *args: str) -> str | None:
    """Run git; return stdout as is, or None if git fails or is missing."""
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return result.stdout if result.returncode == 0 else None


def git(cwd: Path, *args: str) -> str | None:
    output = git_raw(cwd, *args)
    return output.strip() if output is not None else None


def tool_command(tool: str, project: Path) -> list[str]:
    """Prefer the project's own copy of a tool; else the pinned version via uvx."""
    local = project / ".venv" / "bin" / tool
    if local.exists():
        return [str(local)]
    lock = tomllib.loads((PLUGIN_ROOT / "tools.lock.toml").read_text())
    return ["uvx", f"{tool}@{lock['tools'][tool]}"]
