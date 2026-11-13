# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Stop hook: the finish check. Judges everything this session changed.

The base is a snapshot of the working tree taken when the session started, so earlier
commits and uncommitted work that was already there are never blamed on Claude.

Runs in parallel: per-file lint and complexity (unused imports included), new
suppression comments in the diff (catching any added through the shell), types
(basedpyright), tests (pytest), dead code (vulture), class-design rules (pylint)
and dependency hygiene (deptry, warning only). Only findings that are new since
the base count; base results are cached per base.

Loop guards: stop asking once the same problems come back unchanged, as soon as Claude
stops again without changing anything, or after `max_gate_blocks` attempts in one turn.
A prompt containing `plainpy:skip` skips the check for that turn. Files unchanged since
the last complete, clean run skip it too.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

from _common import (
    SKIP_DIRS,
    Settings,
    emit,
    git,
    git_raw,
    is_python_project,
    load_settings,
    project_dir,
    read_hook_input,
    read_json,
    read_session,
    repo_top,
    save_session,
    session_id,
    snapshot,
    state_path,
    tool_command,
    write_json,
)
from _findings import Finding, ToolError, finding_lines, hint_lines, is_test_file, located, new_findings, run_tool
from check_file import Run, base_source, findings_since_base
from guard_edit import FILE_WIDE, SUPPRESSION_HELP, added_suppressions, suppression_problem

TOOL_TIMEOUT = 150
MAX_LINES_PER_SECTION = 15
MAX_REPORT_CHARS = 9000
MAX_CACHED_BASES = 4
WHITELIST = "vulture_whitelist.py"
DOC_SUFFIXES = (".md", ".rst")
# Any of these from a tool run means "this check could not run", never a crash of the whole hook.
CHECK_ERRORS = (ToolError, OSError, ValueError, KeyError, TypeError)
# Cached base results are only valid for the same checks; any edit to this file invalidates them.
CHECKS_VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]

PYLINT_CHECKS = "too-few-public-methods,duplicate-code,too-many-ancestors,too-many-instance-attributes"
PYLINT_OPTIONS = ("--max-parents=3", "--max-attributes=7", "--min-similarity-lines=6", "--output-format=json2")
# Unused variables and attributes are left out: they are mostly dataclass and Pydantic fields,
# read by name somewhere vulture can't see. Unused imports are Ruff's job.
VULTURE_KINDS = ("unused function", "unused class", "unused method", "unused property", "unreachable code")
VULTURE_LINE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+): (?P<kind>unused \w+|unreachable code)(?P<what>.*?) \(\d+% confidence"
)
LINE_RANGE = re.compile(r"\[\d+:\d+\]")
FAILED_TEST = re.compile(r"^(?:FAILED|ERROR) (?P<test>.+?)(?: - .*)?$", re.MULTILINE)

# Without the project's .venv, basedpyright can't see installed packages, so these say more
# about the environment than about the code. deptry still checks imports against declared deps.
ENVIRONMENT_RULES = {"reportMissingImports", "reportMissingModuleSource"}
NO_VENV_NOTE = "no .venv found, so unresolved imports were not checked"
NO_PYTEST_NOTE = "tests not run: no .venv with pytest installed"
TYPES_HINT = "fix the types. Do not add `Any`, `cast()` or `# type: ignore` to silence them."
TESTS_HINT = "make the failing tests pass. Do not delete, skip or weaken them."
DEAD_CODE_HINT = (
    "delete code nothing uses. If it is used dynamically (a framework decorator, entry point "
    "or plugin hook), say so and ask the user to add it to vulture_whitelist.py. Do not delete it."
)


@dataclass(frozen=True)
class Change:
    project: Path
    top: Path  # the repository root
    base: str  # git tree: the working tree when the session started
    current: str  # git tree: the working tree now
    settings: Settings
    files: list[str]  # project-relative paths that differ from the base
    renamed: dict[str, str]  # project-relative new path -> repo-relative path in the base
    baseline_failures: frozenset[str]

    @property
    def prefix(self) -> str:
        """The project's folder inside the repository, "." at the root."""
        return self.project.resolve().relative_to(self.top).as_posix()

    @property
    def python_files(self) -> list[Path]:
        paths = (self.project / name for name in self.files if name.endswith((".py", ".pyi")))
        return [path for path in paths if path.is_file()]


@dataclass
class Section:
    title: str
    findings: list[Finding]
    hint: str = ""
    blocking: bool = True
    notes: list[str] = field(default_factory=list)
    incomplete: bool = False  # a check could not run, so this result is not a clean bill


def failed(title: str, reason: object) -> Section:
    return Section(title, [], blocking=False, notes=[str(reason)], incomplete=True)


# --- the change -------------------------------------------------------------------------


def inside(prefix: str, path: str) -> str | None:
    """A repo-relative path made relative to the project folder; None if it is outside."""
    if prefix == ".":
        return path
    return path[len(prefix) + 1 :] if path.startswith(f"{prefix}/") else None


def pathspec(prefix: str) -> list[str]:
    return [] if prefix == "." else ["--", prefix]


def compare(top: Path, prefix: str, base: str, current: str) -> tuple[list[str], dict[str, str]]:
    """(project-relative paths that differ, renamed files as {new project path: old repo path})."""
    args = ["diff", "--no-ext-diff", "--name-status", "-z", "-M", base, current, *pathspec(prefix)]
    fields = (git_raw(top, *args) or "").split("\0")
    files, renamed = [], {}
    position = 0
    while position + 1 < len(fields):
        status, path = fields[position], fields[position + 1]
        position += 2
        old = None
        if status.startswith(("R", "C")):
            old, path = path, fields[position]
            position += 1
        name = inside(prefix, path)
        if name is None:
            continue
        files.append(name)
        if old is not None and status.startswith("R"):
            renamed[name] = old
    return sorted(files), renamed


def skipped(name: str) -> bool:
    folders = PurePosixPath(name).parts[:-1]
    return any(part in SKIP_DIRS or part.startswith(".") for part in folders)


def python_files_in(change: Change, tree: str) -> list[str]:
    """Project-relative Python files in a snapshot: what git sees, so ignored files never count."""
    listed = git_raw(change.top, "ls-tree", "-r", "-z", "--name-only", tree, *pathspec(change.prefix)) or ""
    names = (inside(change.prefix, name) for name in listed.split("\0") if name)
    return sorted(name for name in names if name and name.endswith((".py", ".pyi")) and not skipped(name))


def extract_base_tree(change: Change, dest: Path) -> Path:
    """Write the base snapshot's files into dest (no hooks, no export rules); return the project's folder."""
    dest.mkdir()
    env = {**os.environ, "GIT_INDEX_FILE": str(dest.with_name("base-index"))}
    args = ["-c", "core.hooksPath=/dev/null", "--work-tree", str(dest), "checkout", change.base, "--", change.prefix]
    if git(change.top, *args, env=env) is None:
        raise ToolError("could not unpack the base snapshot")
    return dest / change.prefix


@cache
def source_of(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def finding_at(root: Path, file: str, line: int, rule: str, message: str) -> Finding:
    path = Path(file)
    relative = path.resolve().relative_to(root.resolve()) if path.is_absolute() else path
    scope, text = located(source_of(root / relative), line)
    return Finding(rule, line, " ".join(message.split()), scope, text, file=relative.as_posix())


# --- checks on a whole tree (run on the working tree and on the base tree) --------------


def types_in(root: Path, files: list[str], change: Change) -> list[Finding]:
    args = [*tool_command("basedpyright", change.project), "--outputjson"]
    if not change.settings.type_warnings:
        args += ["--level", "error"]
    python = change.project / ".venv" / "bin" / "python"
    if python.exists():
        args += ["--pythonpath", str(python)]
    ignored = set() if python.exists() else ENVIRONMENT_RULES
    result = run_tool(args, root, timeout=TOOL_TIMEOUT)
    if result.returncode not in {0, 1}:
        raise ToolError(f"basedpyright failed: {(result.stderr or result.stdout).strip()[:300]}")
    wanted = set(files)
    found = [
        finding_at(root, d["file"], d["range"]["start"]["line"] + 1, d.get("rule") or "type-error", d["message"])
        for d in json.loads(result.stdout)["generalDiagnostics"]
        if d["severity"] in {"error", "warning"} and d.get("rule") not in ignored
    ]
    return [finding for finding in found if finding.file in wanted]


def plain_class(source: str, line: int) -> bool:
    """A class with methods of its own and no base classes: the case R0903 is right about.
    pylint can't see methods inherited from packages it can't import (Pydantic, ORMs),
    and a class with no methods is a record, not a function in disguise."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.lineno == line:
            methods = [n for n in node.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
            return bool(methods) and not node.bases and not node.keywords
    return False


def pylint_finding(root: Path, message: dict[str, Any]) -> Finding:
    if message["messageId"] != "R0801":
        return finding_at(root, message["path"], message["line"], message["messageId"], message["message"])
    # Duplicates are reported on whichever file pylint read last, so the key leaves the file out.
    summary = " ".join(message["message"].splitlines()[:3])
    return Finding("R0801", 0, summary, "", LINE_RANGE.sub("", message["message"]))


def design_in(root: Path, files: list[str], change: Change) -> list[Finding]:
    targets = [f for f in files if f.endswith(".py") and not is_test_file(Path(f))]
    if not targets:
        return []
    args = [*tool_command("pylint", change.project), "--disable=all", f"--enable={PYLINT_CHECKS}", *PYLINT_OPTIONS]
    result = run_tool([*args, "--score=n", *targets], root, timeout=TOOL_TIMEOUT)
    if result.returncode & 33:  # 1 = fatal, 32 = usage error
        raise ToolError(f"pylint failed: {result.stderr.strip()[:300]}")
    found = [pylint_finding(root, m) for m in json.loads(result.stdout)["messages"]]
    return [f for f in found if f.rule != "R0903" or plain_class(source_of(root / f.file), f.line)]


def dead_code_in(root: Path, files: list[str], change: Change) -> list[Finding]:
    targets = [f for f in files if f.endswith(".py")]
    if not targets:
        return []
    if WHITELIST not in targets and (root / WHITELIST).is_file():
        targets.append(WHITELIST)
    args = [*tool_command("vulture", change.project), *targets, "--min-confidence", "60"]
    result = run_tool(args, root, timeout=TOOL_TIMEOUT)
    if result.returncode not in {0, 3}:
        raise ToolError(f"vulture failed: {result.stderr.strip()[:300]}")
    matches = (VULTURE_LINE.match(line) for line in result.stdout.splitlines())
    kept = (
        m
        for m in matches
        if m and m["kind"] in VULTURE_KINDS and not is_test_file(Path(m["file"])) and m["file"] != WHITELIST
    )
    return [
        Finding(m["kind"].replace(" ", "-"), int(m["line"]), what, "", what, file=m["file"])
        for m in kept
        if (what := m["what"].strip() or m["kind"])
    ]


def dependencies_in(root: Path, _files: list[str], change: Change) -> list[Finding]:
    if not (root / "pyproject.toml").exists():
        return []
    handle, output = tempfile.mkstemp(suffix=".json")
    os.close(handle)
    try:
        args = [*tool_command("deptry", change.project), ".", "--json-output", output]
        result = run_tool(args, root, timeout=TOOL_TIMEOUT)
        if result.returncode not in {0, 1}:
            raise ToolError(f"deptry failed: {result.stderr.strip()[:300]}")
        issues = json.loads(Path(output).read_text() or "[]")
    finally:
        Path(output).unlink(missing_ok=True)
    return [
        Finding(
            i["error"]["code"],
            i["location"]["line"] or 0,
            i["error"]["message"],
            "",
            i["module"],
            file=i["location"]["file"],
        )
        for i in issues
    ]


@dataclass(frozen=True)
class TreeCheck:
    title: str
    tool: str
    run: Callable[[Path, list[str], Change], list[Finding]]
    hint: str = ""
    blocking: bool = True


TREE_CHECKS = {
    "types": TreeCheck("Types (basedpyright)", "basedpyright", types_in, TYPES_HINT),
    "design": TreeCheck("Design (pylint)", "pylint", design_in),
    "dead code": TreeCheck("Dead code (vulture)", "vulture", dead_code_in, DEAD_CODE_HINT),
    "dependencies": TreeCheck("Dependencies (deptry), not blocking", "deptry", dependencies_in, blocking=False),
}


def base_cache_key(name: str, change: Change) -> str:
    """Base results stay valid while the base, the tool and its environment, and these checks are the same."""
    command = tool_command(TREE_CHECKS[name].tool, change.project)
    local = Path(command[0])
    installed = local.stat().st_mtime_ns if local.is_absolute() and local.exists() else ""
    venv = (change.project / ".venv" / "bin" / "python").exists()
    parts = (change.base, name, " ".join(command), installed, venv, change.settings.type_warnings, CHECKS_VERSION)
    return "|".join(str(part) for part in parts)


def recent_bases(stored: dict[str, Any], base: str) -> dict[str, Any]:
    """Cached results for this base and the few most recently stored other bases."""
    order = list(dict.fromkeys(key.split("|", 1)[0] for key in stored))
    kept = {*order[-MAX_CACHED_BASES:], base}
    return {key: value for key, value in stored.items() if key.split("|", 1)[0] in kept}


def base_findings(change: Change, workdir: Path) -> dict[str, list[Finding] | ToolError]:
    """Each tree check's findings at the base, from cache or by running it there."""
    cache_file = state_path(change.project).with_name("base-cache.json")
    stored = recent_bases(read_json(cache_file), change.base)
    keys = {name: base_cache_key(name, change) for name in TREE_CHECKS}
    missing = [name for name, key in keys.items() if key not in stored]
    results: dict[str, list[Finding] | ToolError] = {
        name: [Finding(**f) for f in stored[key]] for name, key in keys.items() if key in stored
    }
    if not missing:
        return results
    try:
        root = extract_base_tree(change, workdir / "base")
    except ToolError as err:
        return results | dict.fromkeys(missing, err)
    files = python_files_in(change, change.base)
    with ThreadPoolExecutor() as pool:
        futures = {name: pool.submit(TREE_CHECKS[name].run, root, files, change) for name in missing}
    for name, future in futures.items():
        try:
            found = future.result()
        except CHECK_ERRORS as err:
            results[name] = ToolError(str(err))
            continue
        results[name] = found
        stored[keys[name]] = [asdict(f) for f in found]
    write_json(cache_file, stored)
    return results


# --- checks on the change only ------------------------------------------------------------


def run_for(change: Change, path: Path) -> Run:
    name = path.relative_to(change.project).as_posix()
    ruff, complexipy = tool_command("ruff", change.project), tool_command("complexipy", change.project)
    return Run(path, change.project, change.settings, ruff, complexipy, change.base, change.renamed.get(name))


def lint_and_complexity(change: Change) -> list[Finding]:
    with ThreadPoolExecutor() as pool:
        found = pool.map(lambda path: findings_since_base(run_for(change, path), per_edit=False), change.python_files)
        return [finding for findings in found for finding in findings]


def suppressions(change: Change) -> tuple[list[Finding], list[Finding]]:
    """(suppressions that break the rules, suppressions with a reason for the user to review)."""
    bad, reasoned = [], []
    for path in change.python_files:
        run = run_for(change, path)
        after = path.read_text(errors="replace")
        for comment in added_suppressions(base_source(run) or "", after):
            line = next((n for n, text in enumerate(after.splitlines(), 1) if comment in text), 0)
            problem = suppression_problem(comment)
            reason = "file-wide, never allowed" if FILE_WIDE.search(comment) else "needs a rule code and a reason"
            message = f"`{comment.strip()}` {reason if problem else ''}".strip()
            (bad if problem else reasoned).append(
                Finding("suppression", line, message, "", comment, file=run.shown_path)
            )
    return bad, reasoned


def failing_tests(change: Change, pytest: Path) -> list[Finding]:
    args = [str(pytest), "-q", "-rfE", "-p", "no:cacheprovider"]
    result = run_tool(args, change.project, timeout=TOOL_TIMEOUT)
    if result.returncode not in {0, 1, 2, 5}:
        raise ToolError(f"pytest failed to run: {result.stdout.strip()[-300:]}")
    failed_now = {test.strip() for test in FAILED_TEST.findall(result.stdout)}
    return [Finding("failed", 0, "", "", test, file=test) for test in sorted(failed_now - change.baseline_failures)]


# --- running everything ---------------------------------------------------------------------


def tree_sections(change: Change, workdir: Path) -> list[Section]:
    files = python_files_in(change, change.current)
    with ThreadPoolExecutor() as pool:
        current = {name: pool.submit(check.run, change.project, files, change) for name, check in TREE_CHECKS.items()}
        base = base_findings(change, workdir)
    moved = {old: new for new, path in change.renamed.items() if (old := inside(change.prefix, path))}
    no_venv = not (change.project / ".venv").exists()
    sections = []
    for name, future in current.items():
        check, previous = TREE_CHECKS[name], base[name]
        try:
            found = future.result()
        except CHECK_ERRORS as err:
            sections.append(failed(check.title, err))
            continue
        if isinstance(previous, ToolError):
            sections.append(failed(check.title, f"not compared: the base could not be checked ({previous})"))
            continue
        new = new_findings(found, [replace(f, file=moved.get(f.file, f.file)) for f in previous])
        notes = [NO_VENV_NOTE] if name == "types" and no_venv and new else []
        sections.append(Section(check.title, new, check.hint, check.blocking, notes))
    return sections


def guarded(title: str, run: Callable[[], list[Finding]], hint: str = "") -> Section:
    try:
        return Section(title, run(), hint)
    except CHECK_ERRORS as err:
        return failed(title, err)


def run_checks(change: Change) -> list[Section]:
    pytest = change.project / ".venv" / "bin" / "pytest"
    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor() as pool:
        tree = pool.submit(tree_sections, change, Path(tmp))
        lint = pool.submit(guarded, "Lint and complexity", lambda: lint_and_complexity(change))
        tests = pool.submit(guarded, "Tests (pytest)", lambda: failing_tests(change, pytest), TESTS_HINT)
        bad, reasoned = suppressions(change)
        no_tests = Section("Tests (pytest)", [], blocking=False, notes=[NO_PYTEST_NOTE])
        return [
            lint.result(),
            Section("Suppressions", bad, SUPPRESSION_HELP),
            tests.result() if pytest.exists() else no_tests,
            *tree.result(),
            Section("New suppressions with a reason, for review", reasoned, blocking=False),
        ]


def render(sections: list[Section], header: str) -> str:
    lines = [header]
    for section in sections:
        if not (section.findings or section.notes):
            continue
        entries = finding_lines(section.findings)
        lines.append(f"{section.title}:")
        lines += [f"  {entry}" for entry in entries[:MAX_LINES_PER_SECTION]]
        if len(entries) > MAX_LINES_PER_SECTION:
            lines.append(f"  ...and {len(entries) - MAX_LINES_PER_SECTION} more.")
        lines += [f"  note: {note}" for note in section.notes]
        hints = [f"  how: {section.hint}"] if section.hint else [f"  {h}" for h in hint_lines(section.findings)]
        lines += hints if section.findings else []
    report = "\n".join(lines)
    return report if len(report) <= MAX_REPORT_CHARS else report[:MAX_REPORT_CHARS] + "\n...(cut)"


# --- the hook ---------------------------------------------------------------------------------


def give_up_reason(
    session: dict[str, Any], keys: list[str], hook_input: dict[str, Any], settings: Settings
) -> str | None:
    if hook_input.get("stop_hook_active") and keys == session.get("last"):
        return "the same problems came back unchanged"
    if session.get("blocks", 0) >= settings.max_gate_blocks:
        return f"it already asked {settings.max_gate_blocks} times this turn"
    return None


def decide(
    sections: list[Section], session: dict[str, Any], hook_input: dict[str, Any], settings: Settings
) -> dict[str, Any] | None:
    blocking = [f for s in sections if s.blocking for f in s.findings]
    shown = [s for s in sections if s.findings or s.notes]
    if not blocking:
        return {"systemMessage": render(shown, "plainpy finish check passed, with notes:")} if shown else None
    count = f"{len(blocking)} new problem(s)"
    if not settings.blocking:
        return {"systemMessage": render(shown, f"plainpy ({settings.profile}) finish check: {count}, not blocking.")}
    keys = sorted(str(f.key) for f in blocking)
    reason = give_up_reason(session, keys, hook_input, settings)
    if reason:
        return {"systemMessage": render(shown, f"plainpy stopped asking Claude to fix {count} because {reason}:")}
    session["blocks"] = session.get("blocks", 0) + 1
    session["last"] = keys
    header = f"plainpy ({settings.profile}) finish check: {count} since the session started. Fix them before finishing:"
    return {"decision": "block", "reason": render(shown, header)}


def finish_check(project: Path, session: dict[str, Any], hook_input: dict[str, Any]) -> dict[str, Any] | None:
    base = session.get("base")
    if session.get("skip") or not base or not is_python_project(project):
        return None
    top, current = repo_top(project), snapshot(project)
    if top is None or current is None or current == base:
        return None
    if git(top, "cat-file", "-e", base) is None:
        session["base"] = current
        return {"systemMessage": "plainpy: this session's base snapshot was cleaned up by git, so the finish "
                "check starts over from the files as they are now."}  # fmt: skip
    settings = load_settings(project)
    digest = f"{base}|{current}|{settings}"
    files, renamed = compare(top, project.resolve().relative_to(top).as_posix(), base, current)
    if digest == session.get("clean_digest") or all(name.endswith(DOC_SUFFIXES) for name in files):
        session["clean_digest"] = digest
        return None
    blocked = session.get("blocked") or {}
    if hook_input.get("stop_hook_active") and blocked.get("digest") == digest:
        session["clean_digest"] = digest
        problems = blocked["report"].split("\n", 1)[-1]
        return {"systemMessage": f"plainpy stopped asking because Claude changed nothing since it asked:\n{problems}"}
    baseline = frozenset(session.get("test_baseline", []))
    sections = run_checks(Change(project, top, base, current, settings, files, renamed, baseline))
    outcome = decide(sections, session, hook_input, settings)
    if outcome and "decision" in outcome:
        session["blocked"] = {"digest": digest, "report": outcome["reason"]}
    elif not any(section.incomplete for section in sections):
        session["clean_digest"] = digest
    return outcome


def main() -> None:
    hook_input = read_hook_input()
    project = project_dir(hook_input)
    session_key = session_id(hook_input)
    session = read_session(project, session_key)
    if "base" not in session:
        return
    if not hook_input.get("stop_hook_active"):
        session.update(blocks=0, last=None)
    outcome = finish_check(project, session, hook_input)
    save_session(project, session_key, session)
    if outcome:
        emit(outcome)


if __name__ == "__main__":
    main()
