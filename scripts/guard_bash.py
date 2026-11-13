# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""PreToolUse hook on Bash: ask the user before Claude adds a runtime dependency
or writes to lint config through the shell.

Dev-only installs (`uv add --dev`, `--group`, ...) and installs from existing
requirement files (`pip install -r ...`, `pip install -e .`) pass. Anything
this hook doesn't recognise passes too: it never denies on its own.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from _common import pre_tool_decision, read_hook_input

INSTALLERS = {
    ("uv", "add"), ("poetry", "add"), ("pdm", "add"), ("pip", "install"),
    ("pip3", "install"), ("conda", "install"), ("mamba", "install"),
}  # fmt: skip
DEV_FLAGS = {"--dev", "-D", "-d", "--group", "-G", "--optional"}
FLAGS_WITH_VALUE = {
    "-r", "--requirement", "-c", "--constraint", "-e", "--editable", "-i", "--index-url",
    "--extra-index-url", "--index", "--group", "-G", "--optional", "--python", "-p",
}  # fmt: skip
SEPARATORS = {";", "&&", "||", "|", "&"}
CONFIG_FILES = (
    ".plainpy.toml", "ruff.toml", "pyrightconfig.json", "complexipy.toml",
    "tach.toml", "mypy.ini", "pylintrc", ".coveragerc", "pyproject.toml",
)  # fmt: skip
WRITERS = {"tee", "mv", "cp", "rm", "truncate", "ln"}
IN_PLACE_EDITORS = {"sed", "perl"}
REDIRECT_TARGET = re.compile(r">>?\s*['\"]?([^\s'\";&|]+)")


def split_commands(command: str) -> list[list[str]]:
    """Split a shell line into simple commands at ; && || | &."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return [command.split()]
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token in SEPARATORS:
            commands.append([])
        else:
            commands[-1].append(token)
    return [words for words in commands if words]


def is_package(word: str) -> bool:
    return not word.startswith(("-", ".", "/")) and "/" not in word


def installed_packages(words: list[str]) -> list[str]:
    """Runtime packages a simple command would add; [] if none or dev-only."""
    verb_at = next((i for i in range(len(words) - 1) if (Path(words[i]).name, words[i + 1]) in INSTALLERS), None)
    if verb_at is None:
        return []
    args = words[verb_at + 2 :]
    if DEV_FLAGS.intersection(args):
        return []
    values = {args[i + 1] for i, arg in enumerate(args[:-1]) if arg in FLAGS_WITH_VALUE}
    return [arg for arg in args if is_package(arg) and arg not in values]


def config_name(word: str) -> str | None:
    return next((name for name in CONFIG_FILES if Path(word).name == name), None)


def written_files(words: list[str]) -> list[str]:
    """Config files a simple command writes: `sed -i`, `perl -i`, tee, mv, cp, rm, ..."""
    program = Path(words[0]).name
    in_place = program in IN_PLACE_EDITORS and any(w.startswith(("-i", "-pi")) for w in words)
    if not (in_place or program in WRITERS):
        return []
    return [name for word in words[1:] if (name := config_name(word))]


def touched_config(command: str) -> list[str]:
    redirected = [name for target in REDIRECT_TARGET.findall(command) if (name := config_name(target))]
    written = [name for words in split_commands(command) for name in written_files(words)]
    return sorted(set(redirected + written))


def main() -> None:
    command = read_hook_input().get("tool_input", {}).get("command", "")
    packages = [package for words in split_commands(command) for package in installed_packages(words)]
    if packages:
        pre_tool_decision("ask", f"plainpy: Claude wants to add runtime dependencies: {', '.join(packages)}. Allow it?")
        return
    files = touched_config(command)
    if files:
        pre_tool_decision("ask", f"plainpy: this shell command may change {', '.join(files)}. Allow it?")


if __name__ == "__main__":
    main()
