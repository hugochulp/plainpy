# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""PostToolUse hook: autofix, lint and complexity-check one edited Python file.

Only findings that are new since the base revision block: a finding that
already existed at the base (same rule, same enclosing function, same line
text) is skipped, and a size or complexity finding is skipped unless its
number went up.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from _common import (
    Settings,
    emit,
    git,
    git_raw,
    load_settings,
    project_dir,
    read_hook_input,
    read_state,
    tool_command,
)

RUFF_RULES = (
    "E4", "E7", "E9", "W", "F", "I", "B", "UP", "SIM", "C4", "C90", "PL", "RET", "ARG",
    "ERA", "FBT001", "FBT002", "ANN", "BLE", "TRY", "PIE", "RUF", "PTH", "S",
)  # fmt: skip
RUFF_LIMITS = (
    "lint.pylint.max-args=5",
    "lint.pylint.max-positional-args=5",
    "lint.pylint.max-branches=10",
    "lint.pylint.max-returns=6",
    "lint.pylint.max-statements=40",
)
# TRY003 pushes toward one exception class per message; PLR2004 toward constant sprawl.
# F401 is left to the finish check: an import is often added one edit before its use.
IGNORED = ("TRY003", "PLR2004", "F401")
IGNORED_IN_TESTS = ("S101", "S105", "S106", "ANN", "ARG", "PLR2004")
METRIC = re.compile(r"\((\d+) > (\d+)\)")
MAX_SHOWN = 20
MAX_ROWS_LISTED = 6

COMPLEXITY_HINT = (
    "flatten it: early returns, guard clauses, a dict lookup, or drop cases nobody "
    "asked for. Do not move the logic into single-use helpers or new classes."
)
HINTS = {
    "CPX001": COMPLEXITY_HINT,
    "C901": COMPLEXITY_HINT,
    "PLR0911": COMPLEXITY_HINT,
    "PLR0912": COMPLEXITY_HINT,
    "PLR0913": "remove parameters callers never vary. Do not bundle them into a config class.",
    "PLR0917": "remove parameters callers never vary, or make the rest keyword-only.",
    "PLR0915": "remove what the task does not need before splitting anything.",
}


class ToolError(Exception):
    pass


@dataclass(frozen=True)
class Finding:
    rule: str
    line: int
    message: str
    scope: str
    text: str
    value: int | None = None

    @property
    def key(self) -> tuple[str, ...]:
        if self.value is not None:
            return (self.rule, self.scope)
        return (self.rule, self.scope, self.text)


@dataclass(frozen=True)
class Run:
    path: Path
    project: Path
    settings: Settings
    ruff: list[str]
    complexipy: list[str]


def edited_python_file(hook_input: dict[str, Any]) -> Path | None:
    file_path = hook_input.get("tool_input", {}).get("file_path")
    if not file_path:
        return None
    path = Path(file_path)
    return path if path.suffix in {".py", ".pyi"} and path.is_file() else None


def is_test_file(path: Path) -> bool:
    name = path.name
    in_tests_dir = "tests" in path.parts or "test" in path.parts
    return in_tests_dir or name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def scope_spans(source: str) -> list[tuple[int, int, str]]:
    """(first line, last line, qualified name) for every function and class."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans: list[tuple[int, int, str]] = []
    pending: list[tuple[ast.AST, str]] = [(tree, "")]
    while pending:
        node, prefix = pending.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{prefix}{child.name}"
                first = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                spans.append((first, child.end_lineno or child.lineno, name))
                pending.append((child, f"{name}."))
            else:
                pending.append((child, prefix))
    return spans


def scope_of(line: int, spans: list[tuple[int, int, str]]) -> str:
    enclosing = [span for span in spans if span[0] <= line <= span[1]]
    return max(enclosing)[2] if enclosing else "<module>"


def run_tool(args: list[str], cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, cwd=cwd, input=stdin, capture_output=True, text=True, check=False)
    except FileNotFoundError as err:
        raise ToolError(f"could not start {args[0]} (is uv installed?)") from err


def ruff_options(settings: Settings) -> list[str]:
    options = ["--extend-select", ",".join(RUFF_RULES)]
    limits = [*RUFF_LIMITS, f"lint.mccabe.max-complexity={settings.mccabe_max}"]
    for limit in limits:
        options += ["--config", limit]
    return options


def autofix(run: Run) -> None:
    """Apply safe fixes, then format. F401 is never fixed: Claude often adds an
    import one edit before the code that uses it."""
    target = str(run.path)
    fix = [*run.ruff, "check", *ruff_options(run.settings), "--fix", "--unfixable", "F401"]
    run_tool([*fix, "--force-exclude", "--quiet", "--exit-zero", target], run.project)
    run_tool([*run.ruff, "format", "--force-exclude", "--quiet", target], run.project)


def ruff_findings(run: Run, source: str) -> list[Finding]:
    args = [*run.ruff, "check", *ruff_options(run.settings), "--no-fix", "--force-exclude"]
    args += ["--output-format", "json", "--stdin-filename", str(run.path), "-"]
    result = run_tool(args, run.project, stdin=source)
    if result.returncode not in {0, 1}:
        raise ToolError(f"ruff failed: {result.stderr.strip()[:500]}")
    spans, lines = scope_spans(source), source.splitlines()
    ignored = IGNORED + (IGNORED_IN_TESTS if is_test_file(run.path) else ())
    findings = []
    for item in json.loads(result.stdout or "[]"):
        rule = item["code"] or "syntax-error"
        if rule.startswith(ignored):
            continue
        line = item["location"]["row"]
        metric = METRIC.search(item["message"])
        text = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        value = int(metric.group(1)) if metric else None
        findings.append(Finding(rule, line, item["message"], scope_of(line, spans), text, value))
    return findings


def complexity_findings(run: Run, source: str, workdir: Path) -> list[Finding]:
    target, output = workdir / run.path.name, workdir / "complexipy.json"
    target.write_text(source)
    args = [*run.complexipy, str(target), "--output-format", "json", "--output", str(output)]
    run_tool([*args, "--quiet", "--cache-dir", str(workdir / "cache")], workdir)
    if not output.exists():
        raise ToolError("complexipy produced no output")
    limit = run.settings.cognitive_max
    return [
        Finding(
            rule="CPX001",
            line=item["line_start"],
            message=f"`{item['function_name']}` has cognitive complexity {item['complexity']} > {limit}",
            scope=item["function_name"].replace("::", "."),
            text="",
            value=item["complexity"],
        )
        for item in json.loads(output.read_text())
        if item["complexity"] > limit
    ]


def collect(run: Run, source: str, workdir: Path) -> list[Finding]:
    workdir.mkdir()
    return ruff_findings(run, source) + complexity_findings(run, source, workdir)


def base_source(run: Run) -> str | None:
    base = read_state(run.project).get("base")
    top = git(run.path.parent, "rev-parse", "--show-toplevel") if base else None
    if top is None:
        return None
    relative = run.path.resolve().relative_to(Path(top).resolve()).as_posix()
    return git_raw(Path(top), "show", f"{base}:{relative}")


def is_new(finding: Finding, base_counts: Counter[tuple[str, ...]], base_values: dict[tuple[str, ...], int]) -> bool:
    if finding.value is not None:
        return finding.value > base_values.get(finding.key, -1)
    if base_counts[finding.key] > 0:
        base_counts[finding.key] -= 1
        return False
    return True


def new_findings(current: list[Finding], base: list[Finding]) -> list[Finding]:
    base_counts = Counter(f.key for f in base if f.value is None)
    base_values = {f.key: f.value for f in base if f.value is not None}
    return [f for f in current if is_new(f, base_counts, base_values)]


def check(run: Run) -> list[Finding]:
    autofix(run)
    source = run.path.read_text()
    base = base_source(run)
    with TemporaryDirectory() as tmp, ThreadPoolExecutor() as pool:
        current = pool.submit(collect, run, source, Path(tmp) / "current")
        previous = pool.submit(collect, run, base, Path(tmp) / "base") if base is not None else None
        return new_findings(current.result(), previous.result() if previous else [])


def group_line(rule: str, group: list[Finding], shown_path: str) -> str:
    """One line per rule: `path:12 RULE msg`, or `path RULE x3 at lines 4, 9, 15: first msg`."""
    rows = sorted({f.line for f in group})
    if len(group) == 1:
        return f"{shown_path}:{rows[0]} {rule} {group[0].message}"
    listed = ", ".join(str(row) for row in rows[:MAX_ROWS_LISTED])
    more = ", ..." if len(rows) > MAX_ROWS_LISTED else ""
    where = f"line{'s' if len(rows) > 1 else ''} {listed}{more}"
    return f"{shown_path} {rule} x{len(group)} at {where}: {group[0].message}"


def finding_lines(findings: list[Finding], shown_path: str) -> list[str]:
    """Size and complexity findings one per function, first; other rules grouped."""
    ordered = sorted(findings, key=lambda f: (f.value is None, f.line))
    lines = [f"{shown_path}:{f.line} {f.rule} {f.message}" for f in ordered if f.value is not None]
    groups: dict[str, list[Finding]] = {}
    for finding in ordered:
        if finding.value is None:
            groups.setdefault(finding.rule, []).append(finding)
    return lines + [group_line(rule, group, shown_path) for rule, group in groups.items()]


def hint_lines(findings: list[Finding]) -> list[str]:
    """Each fix hint once, with the rules it applies to."""
    rules_by_hint: dict[str, list[str]] = {}
    for finding in sorted(findings, key=lambda f: f.line):
        hint = HINTS.get(finding.rule)
        if hint is not None and finding.rule not in rules_by_hint.setdefault(hint, []):
            rules_by_hint[hint].append(finding.rule)
    return [f"- {', '.join(rules)}: {hint}" for hint, rules in rules_by_hint.items()]


def format_report(findings: list[Finding], run: Run) -> str:
    try:
        shown_path = run.path.resolve().relative_to(run.project).as_posix()
    except ValueError:
        shown_path = str(run.path)
    header = f"plainpy ({run.settings.profile}): {len(findings)} new problem(s) in {shown_path}. Fix them now:"
    entries = finding_lines(findings, shown_path)
    lines = [header, *entries[:MAX_SHOWN]]
    if len(entries) > MAX_SHOWN:
        lines.append(f"...and {len(entries) - MAX_SHOWN} more.")
    hints = hint_lines(findings)
    if hints:
        lines += ["How to fix:", *hints]
    return "\n".join(lines)


def main() -> int:
    hook_input = read_hook_input()
    path = edited_python_file(hook_input)
    if path is None:
        return 0
    project = project_dir(hook_input)
    run = Run(path, project, load_settings(project), tool_command("ruff", project), tool_command("complexipy", project))
    try:
        findings = check(run)
    except ToolError as err:
        emit({"systemMessage": f"plainpy: check skipped, {err}"})
        return 0
    if not findings:
        return 0
    report = format_report(findings, run)
    if run.settings.blocking:
        print(report, file=sys.stderr)
        return 2
    emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": report}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
