"""Structural checks on the plugin and marketplace manifests."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(relative: str) -> dict[str, object]:
    return json.loads((ROOT / relative).read_text())


def test_versions_agree() -> None:
    plugin = load(".claude-plugin/plugin.json")
    marketplace = load(".claude-plugin/marketplace.json")
    assert marketplace["plugins"][0]["version"] == plugin["version"]


def test_hook_scripts_exist() -> None:
    hooks = load("hooks/hooks.json")["hooks"]
    commands = [hook["command"] for groups in hooks.values() for group in groups for hook in group["hooks"]]
    for command in commands:
        script = command.split("${CLAUDE_PLUGIN_ROOT}/")[1].rstrip('"')
        assert (ROOT / script).is_file(), script


def test_every_tool_is_pinned() -> None:
    lock = tomllib.loads((ROOT / "tools.lock.toml").read_text())["tools"]
    assert set(lock) == {"ruff", "complexipy"}
    assert all(version.count(".") == 2 for version in lock.values())
