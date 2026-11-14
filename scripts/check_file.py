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

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from _common import (
    PYTHON_SUFFIXES,
    Settings,
    compare,
    emit,
    git,
    git_raw,
    load_settings,
    pathspec,
    project_dir,
    read_hook_input,
    read_session,
    repo_top,
    session_id,
    snapshot,
    tool_command,
)
from _findings import (
    Finding,
    ToolError,
    finding_lines,
    hint_lines,
    is_test_file,
    new_findings,
    run_tool,
    scope_of,
    scope_spans,
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
IGNORED = ("TRY003", "PLR2004")
# An import is often added one edit before its use, so F401 waits for the finish check.
IGNORED_PER_EDIT = ("F401",)
IGNORED_IN_TESTS = ("S101", "S105", "S106", "ANN", "ARG", "PLR2004")
# Imports in a package's __init__.py are usually re-exports.
IGNORED_IN_INIT = ("F401",)
METRIC = re.compile(r"\((\d+) > (\d+)\)")
MAX_SHOWN = 20
# Per tool call. Ruff takes milliseconds; this only bounds a hang, well inside the hook's 60 s.
TOOL_TIMEOUT = 15


@dataclass(frozen=True)
class Run:
    path: Path
    project: Path
    settings: Settings
    ruff: list[str]
    complexipy: list[str]
    base: str | None = None  # the session's base snapshot (a git tree)
    base_name: str | None = None  # the file's project-relative path in the base, if it was renamed
    top: Path | None = None  # the repository root

    @property
    def real_path(self) -> Path:
        """The path with its folders resolved, but not the file itself, so a symlink stays where it is."""
        return self.path.parent.resolve() / self.path.name

    @property
    def shown_path(self) -> str:
        try:
            return self.real_path.relative_to(self.project).as_posix()
        except ValueError:
            return str(self.path)


def edited_python_file(hook_input: dict[str, Any]) -> Path | None:
    file_path = hook_input.get("tool_input", {}).get("file_path")
    if not file_path:
        return None
    path = Path(file_path)
    return path if path.suffix in PYTHON_SUFFIXES and path.is_file() else None


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
    run_tool([*fix, "--force-exclude", "--quiet", "--exit-zero", target], run.project, timeout=TOOL_TIMEOUT)
    run_tool([*run.ruff, "format", "--force-exclude", "--quiet", target], run.project, timeout=TOOL_TIMEOUT)


def ruff_findings(run: Run, source: str, *, per_edit: bool) -> list[Finding]:
    args = [*run.ruff, "check", *ruff_options(run.settings), "--no-fix", "--force-exclude"]
    args += ["--output-format", "json", "--stdin-filename", str(run.path), "-"]
    result = run_tool(args, run.project, stdin=source, timeout=TOOL_TIMEOUT)
    if result.returncode not in {0, 1}:
        raise ToolError(f"ruff failed: {result.stderr.strip()[:500]}")
    spans, lines = scope_spans(source), source.splitlines()
    ignored = (
        IGNORED
        + (IGNORED_PER_EDIT if per_edit else ())
        + (IGNORED_IN_TESTS if is_test_file(run.path) else ())
        + (IGNORED_IN_INIT if run.path.name == "__init__.py" else ())
    )
    findings = []
    for item in json.loads(result.stdout or "[]"):
        rule = item["code"] or "syntax-error"
        if rule.startswith(ignored):
            continue
        line = item["location"]["row"]
        metric = METRIC.search(item["message"])
        text = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        value = int(metric.group(1)) if metric else None
        findings.append(Finding(rule, line, item["message"], scope_of(line, spans), text, value, run.shown_path))
    return findings


def complexity_findings(run: Run, source: str, workdir: Path) -> list[Finding]:
    target, output = workdir / run.path.name, workdir / "complexipy.json"
    target.write_text(source)
    args = [*run.complexipy, str(target), "--output-format", "json", "--output", str(output)]
    run_tool([*args, "--quiet", "--cache-dir", str(workdir / "cache")], workdir, timeout=TOOL_TIMEOUT)
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
            file=run.shown_path,
        )
        for item in json.loads(output.read_text())
        if item["complexity"] > limit
    ]


def collect(run: Run, source: str, workdir: Path, *, per_edit: bool) -> list[Finding]:
    workdir.mkdir()
    return ruff_findings(run, source, per_edit=per_edit) + complexity_findings(run, source, workdir)


def base_source(run: Run) -> str | None:
    if run.base is None or run.top is None:
        return None
    path = run.project / run.base_name if run.base_name else run.real_path
    try:
        name = path.relative_to(run.top).as_posix()
    except ValueError:  # outside the repository
        return None
    return git_raw(run.top, "show", f"{run.base}:{name}")


def renamed_from(run: Run) -> str | None:
    """The file's project-relative path in the base, if this session renamed it (e.g. by a shell `mv`)."""
    if run.base is None or run.top is None:
        return None
    prefix = run.project.relative_to(run.top).as_posix()
    deleted = git(run.top, "diff-index", "--name-only", "--diff-filter=D", run.base, *pathspec(prefix))
    if not deleted:  # nothing from the base is missing, so nothing was renamed: skip the snapshot
        return None
    current = snapshot(run.project)
    return compare(run.top, prefix, run.base, current)[1].get(run.shown_path) if current else None


def findings_since_base(run: Run, *, per_edit: bool) -> list[Finding]:
    """Findings in the file now that were not there at the base revision."""
    source = run.path.read_text(errors="replace")
    base = base_source(run)
    with TemporaryDirectory() as tmp, ThreadPoolExecutor() as pool:
        current = pool.submit(collect, run, source, Path(tmp) / "current", per_edit=per_edit)
        previous = pool.submit(collect, run, base, Path(tmp) / "base", per_edit=per_edit) if base is not None else None
        return new_findings(current.result(), previous.result() if previous else [])


def check(run: Run) -> list[Finding]:
    autofix(run)
    return findings_since_base(run, per_edit=True)


def format_report(findings: list[Finding], run: Run) -> str:
    header = f"plainpy ({run.settings.profile}): {len(findings)} new problem(s) in {run.shown_path}. Fix them now:"
    entries = finding_lines(findings)
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
    base = read_session(project, session_id(hook_input)).get("base")
    ruff, complexipy = tool_command("ruff", project), tool_command("complexipy", project)
    run = Run(path, project, load_settings(project), ruff, complexipy, base, top=repo_top(project))
    if base and base_source(run) is None:  # a new file, or one renamed this session
        run = replace(run, base_name=renamed_from(run))
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
