"""Findings shared by the per-edit and finish checks: fingerprints, base comparison, report lines.

A finding is "new" unless the same one existed at the base revision: same rule, file,
enclosing function and line text. A size or complexity finding carries a number and is
new only if that number went up.
"""

from __future__ import annotations

import ast
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

MAX_ROWS_LISTED = 6
SHORT_MESSAGE = 40

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
    "F401": "delete imports nothing uses.",
    "R0903": "a class with one method and no real state should be a function.",
    "R0902": "a class holding this many attributes is doing too much; drop what the task does not need.",
    "R0901": "flatten the inheritance; prefer composition or plain functions.",
    "R0801": "keep one copy of the duplicated code, next to its callers.",
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
    file: str = ""

    @property
    def key(self) -> tuple[str, ...]:
        if self.value is not None:
            return (self.rule, self.file, self.scope)
        return (self.rule, self.file, self.scope, self.text)

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.line > 0 else self.file


def run_tool(
    args: list[str], cwd: Path, stdin: str | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args, cwd=cwd, input=stdin, capture_output=True, text=True, errors="replace", check=False, timeout=timeout
        )
    except FileNotFoundError as err:
        raise ToolError(f"could not start {args[0]} (is uv installed?)") from err
    except subprocess.TimeoutExpired as err:
        raise ToolError(f"{Path(args[0]).name} timed out after {timeout:.0f} s") from err


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


def located(source: str, line: int) -> tuple[str, str]:
    """(enclosing function or class, stripped line text) for a 1-based line."""
    lines = source.splitlines()
    text = lines[line - 1].strip() if 0 < line <= len(lines) else ""
    return scope_of(line, scope_spans(source)), text


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


def group_line(group: list[Finding]) -> str:
    """One line per file and rule: `path:12 RULE msg`, or `path RULE x3 at lines 4, 9, 15: msg`.
    Short messages that differ (names, mostly) are all listed; long ones show the first."""
    first = group[0]
    rows = sorted({f.line for f in group if f.line > 0})
    messages = list(dict.fromkeys(f.message for f in group))
    if not rows:  # whole-file findings, e.g. duplicate code or a dependency: list each message
        shown = "; ".join(messages[:MAX_ROWS_LISTED]) + ("; ..." if len(messages) > MAX_ROWS_LISTED else "")
        return f"{first.location} {first.rule} {shown}".strip()
    if len(group) == 1:
        return f"{first.location} {first.rule} {first.message}".strip()
    listed = ", ".join(str(row) for row in rows[:MAX_ROWS_LISTED])
    more = ", ..." if len(rows) > MAX_ROWS_LISTED else ""
    where = f"line{'s' if len(rows) > 1 else ''} {listed}{more}"
    short = all(len(m) <= SHORT_MESSAGE for m in messages)
    shown = ", ".join(messages[:MAX_ROWS_LISTED]) if short else first.message
    return f"{first.file} {first.rule} x{len(group)} at {where}: {shown}"


def finding_lines(findings: list[Finding]) -> list[str]:
    """Size and complexity findings one per function, first; other rules grouped per file."""
    ordered = sorted(findings, key=lambda f: (f.value is None, f.file, f.line))
    lines = [f"{f.location} {f.rule} {f.message}".rstrip() for f in ordered if f.value is not None]
    groups: dict[tuple[str, str], list[Finding]] = {}
    for finding in ordered:
        if finding.value is None:
            groups.setdefault((finding.file, finding.rule), []).append(finding)
    return lines + [group_line(group) for group in groups.values()]


def hint_lines(findings: list[Finding]) -> list[str]:
    """Each fix hint once, with the rules it applies to."""
    rules_by_hint: dict[str, list[str]] = {}
    for finding in sorted(findings, key=lambda f: (f.file, f.line)):
        hint = HINTS.get(finding.rule)
        if hint is not None and finding.rule not in rules_by_hint.setdefault(hint, []):
            rules_by_hint[hint].append(finding.rule)
    return [f"- {', '.join(rules)}: {hint}" for hint, rules in rules_by_hint.items()]
