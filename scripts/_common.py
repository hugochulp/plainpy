"""Shared helpers for plainpy hook scripts: hook I/O, settings, state, git, tools."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

PROFILES: dict[str, dict[str, Any]] = {
    "lenient": {"blocking": False, "cognitive_max": 25, "mccabe_max": 15, "type_warnings": False},
    "standard": {"blocking": True, "cognitive_max": 15, "mccabe_max": 10, "type_warnings": False},
    "strict": {"blocking": True, "cognitive_max": 10, "mccabe_max": 8, "type_warnings": True},
}
DEFAULT_MAX_GATE_BLOCKS = 3
KEPT_SESSIONS = 20
# Folders never analysed or snapshotted: virtualenvs, build output, caches.
SKIP_DIRS = frozenset({".venv", "venv", "env", "node_modules", "build", "dist", "__pycache__", ".git", ".tox", ".nox"})
# Untracked files bigger than this are left out of snapshots (data dumps, binaries).
MAX_SNAPSHOT_FILE_BYTES = 1_000_000


# Files whose only job is configuring checks; any change to them needs the user's approval.
# pyproject.toml is judged per [tool.*] section instead (see guard_edit).
CONFIG_FILES = frozenset(
    {
        ".plainpy.toml", "ruff.toml", ".ruff.toml", "pyrightconfig.json", "complexipy.toml",
        "tach.toml", "mypy.ini", ".mypy.ini", ".pylintrc", "pylintrc", ".coveragerc",
        "vulture_whitelist.py",
    }
)  # fmt: skip


@dataclass(frozen=True)
class Settings:
    profile: str
    blocking: bool
    cognitive_max: int
    mccabe_max: int
    type_warnings: bool = False
    max_gate_blocks: int = DEFAULT_MAX_GATE_BLOCKS


def read_hook_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


def pre_tool_decision(decision: str, reason: str) -> None:
    """Answer a PreToolUse hook: "deny" goes back to Claude, "ask" prompts the user."""
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        }
    )


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
    values = PROFILES[profile] | {"max_gate_blocks": DEFAULT_MAX_GATE_BLOCKS} | config.get("overrides", {})
    return Settings(
        profile=profile,
        blocking=bool(values["blocking"]),
        cognitive_max=int(values["cognitive_max"]),
        mccabe_max=int(values["mccabe_max"]),
        type_warnings=bool(values["type_warnings"]),
        max_gate_blocks=int(values["max_gate_blocks"]),
    )


def session_id(hook_input: dict[str, Any]) -> str:
    return str(hook_input.get("session_id") or "default")


def state_path(project: Path) -> Path:
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA")
    root = Path(data_dir) if data_dir else Path.home() / ".cache" / "plainpy"
    digest = hashlib.sha256(str(project).encode()).hexdigest()[:16]
    return root / "projects" / digest / "state.json"


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write via a temp file and rename, so a concurrent reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, indent=2))
    temp.replace(path)


def read_session(project: Path, session: str) -> dict[str, Any]:
    return read_json(state_path(project)).get("sessions", {}).get(session, {})


def save_session(project: Path, session: str, values: dict[str, Any]) -> None:
    """Store one session's state. Re-reads the file first so other sessions' updates survive."""
    path = state_path(project)
    sessions = read_json(path).get("sessions", {})
    sessions.pop(session, None)
    sessions[session] = values
    write_json(path, {"sessions": dict(list(sessions.items())[-KEPT_SESSIONS:])})


def git_raw(cwd: Path, *args: str, env: dict[str, str] | None = None, stdin: str | None = None) -> str | None:
    """Run git; return stdout as is, or None if git fails or is missing."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, input=stdin, env=env, capture_output=True, text=True, errors="replace", check=False
        )
    except (FileNotFoundError, NotADirectoryError):
        return None
    return result.stdout if result.returncode == 0 else None


def git(cwd: Path, *args: str, env: dict[str, str] | None = None, stdin: str | None = None) -> str | None:
    output = git_raw(cwd, *args, env=env, stdin=stdin)
    return output.strip() if output is not None else None


def repo_top(project: Path) -> Path | None:
    top = git(project, "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else None


def snapshot(project: Path) -> str | None:
    """A git tree object of the working tree as it is now: tracked files plus untracked,
    non-ignored ones (minus virtualenv-like folders and big files). Built in a scratch
    index, so the user's staging area, branches and history are untouched."""
    top = repo_top(project)
    index = git(project, "rev-parse", "--git-path", "index")
    if top is None or index is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "index"
        real_index = project / index
        if real_index.is_file():
            # Reuse git's file-status cache, which keeps this fast on big repos. copy2 keeps the
            # index's timestamp, which git needs to spot files edited in the same second.
            shutil.copy2(real_index, scratch)
        env = {**os.environ, "GIT_INDEX_FILE": str(scratch)}
        if git(top, "add", "--update", env=env) is None:
            return None
        excluded = [arg for name in sorted(SKIP_DIRS) for arg in ("--exclude", name)]
        listed = git_raw(top, "ls-files", "-z", "--others", "--exclude-standard", *excluded, env=env) or ""
        untracked = [
            name
            for name in listed.split("\0")
            if name and (top / name).is_file() and (top / name).stat().st_size <= MAX_SNAPSHOT_FILE_BYTES
        ]
        if untracked:
            add = ["--literal-pathspecs", "add", "--pathspec-from-file=-", "--pathspec-file-nul"]
            if git(top, *add, env=env, stdin="\0".join(untracked)) is None:
                return None
        return git(top, "write-tree", env=env)


def tool_command(tool: str, project: Path) -> list[str]:
    """Prefer the project's own copy of a tool; else the pinned version via uvx."""
    local = project / ".venv" / "bin" / tool
    if local.exists():
        return [str(local)]
    lock = tomllib.loads((PLUGIN_ROOT / "tools.lock.toml").read_text())
    return ["uvx", "--python", lock["python"]["version"], f"{tool}@{lock['tools'][tool]}"]
