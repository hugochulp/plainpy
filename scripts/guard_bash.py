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

import shlex
from itertools import takewhile
from pathlib import Path

from _common import CONFIG_FILES, pre_tool_decision, read_hook_input

INSTALLERS = {
    ("uv", "add"), ("poetry", "add"), ("pdm", "add"), ("pip", "install"),
    ("pip3", "install"), ("conda", "install"), ("mamba", "install"),
}  # fmt: skip
DEV_FLAGS = {"--dev", "-D", "-d", "--group", "-G", "--optional"}
FLAGS_WITH_VALUE = {
    "-r", "--requirement", "-c", "--constraint", "-e", "--editable", "-i", "--index-url",
    "--extra-index-url", "--index", "--group", "-G", "--optional", "--python", "-p",
}  # fmt: skip
SHELL_PROTECTED = CONFIG_FILES | {"pyproject.toml"}
WRITERS = {"tee", "mv", "cp", "rm", "truncate", "ln"}
IN_PLACE_EDITORS = {"sed", "perl"}
OPERATOR_CHARS = "();<>|&\n"


def shell_tokens(command: str) -> list[str]:
    """Quote-aware words. Unquoted operators (; && | > >> ...) and newlines become their
    own tokens; quoted text, newlines inside quotes included, stays inside its word."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=OPERATOR_CHARS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return command.split()


def is_operator(token: str) -> bool:
    return bool(token) and set(token) <= set(OPERATOR_CHARS)


def is_redirect(token: str) -> bool:
    return is_operator(token) and ">" in token


def split_commands(tokens: list[str]) -> list[list[str]]:
    """Simple commands, split at ; && || | & ( ) and unquoted newlines. Redirections stay
    inside their command."""
    commands: list[list[str]] = [[]]
    for token in tokens:
        if is_operator(token) and not set(token) & set("<>"):
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
    args = list(takewhile(lambda word: not is_operator(word), words[verb_at + 2 :]))
    if DEV_FLAGS.intersection(args):
        return []
    values = {args[i + 1] for i, arg in enumerate(args[:-1]) if arg in FLAGS_WITH_VALUE}
    return [arg for arg in args if is_package(arg) and not arg.isdigit() and arg not in values]


def config_name(word: str) -> str | None:
    name = Path(word).name
    return name if name in SHELL_PROTECTED else None


def written_files(words: list[str]) -> list[str]:
    """Config files a simple command writes: `sed -i`, `perl -i`, tee, mv, cp, rm, ..."""
    program = Path(words[0]).name
    in_place = program in IN_PLACE_EDITORS and any(w.startswith(("-i", "-pi")) for w in words)
    if not (in_place or program in WRITERS):
        return []
    return [name for word in words[1:] if (name := config_name(word))]


def touched_config(tokens: list[str], commands: list[list[str]]) -> list[str]:
    targets = [tokens[i + 1] for i, token in enumerate(tokens[:-1]) if is_redirect(token)]
    redirected = [name for target in targets if (name := config_name(target))]
    written = [name for words in commands for name in written_files(words)]
    return sorted(set(redirected + written))


def main() -> None:
    tokens = shell_tokens(read_hook_input().get("tool_input", {}).get("command", ""))
    commands = split_commands(tokens)
    packages = [package for words in commands for package in installed_packages(words)]
    if packages:
        pre_tool_decision("ask", f"plainpy: Claude wants to add runtime dependencies: {', '.join(packages)}. Allow it?")
        return
    files = touched_config(tokens, commands)
    if files:
        pre_tool_decision("ask", f"plainpy: this shell command may change {', '.join(files)}. Allow it?")


if __name__ == "__main__":
    main()
