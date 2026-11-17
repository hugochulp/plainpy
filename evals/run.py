"""Run SlopCodeBench problems through Claude Code, with and without plainpy, one session per checkpoint.

Each checkpoint is a fresh, sandboxed `claude -p` session on what the last one left, as in SlopCodeBench; its
solution is scored by scb-check and every hidden test file so far. Rows go to <out>/results.jsonl for measure.py."""

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBLEMS = HERE / ".cache" / "scb-problems"  # gabeorlanski/scb-problems @ 9cd9ca3, solutions left out
PLUGIN = HERE / ".cache" / "plainpy-v0.4.1"  # `git worktree add --detach evals/.cache/plainpy-v0.4.1 v0.4.1`
SCB_CHECK = "scb-check@0.2.0"
TOOLS = "Bash,Read,Edit,Write,Glob,Grep,Skill"  # coding tools only: no web, MCP servers or app tools
# What gets scored and tested: no venv, git data, or tests Claude wrote for itself (as plainpy's is_test_file).
SOLUTION_ONLY = shutil.ignore_patterns(
    ".venv", ".git", "__pycache__", "test", "tests", "test_*.py", "*_test.py", "conftest.py"
)
GIT = ["git", "-c", "user.name=plainpy-eval", "-c", "user.email=eval@localhost"]

# SlopCodeBench's just-solve prompt (configs/prompts/just-solve.jinja), word for word.
FIRST = "Use a virtual environment and ensure that a `requirements.txt` is present with any dependencies you need to solve the problem."
LATER = "Keep using the same virtual environment you started with, update `requirements.txt` with any new dependencies you need."
PROMPT = (
    "Implement a program that 100% solves the specification. That is all you need to do.\n{env}\nYour task is:\n{spec}"
)


def settings(hidden: list[Path]) -> str:
    """Bash is sandboxed to the workspace and PyPI; the hidden tests and the other runs can't be read."""
    sandbox = {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False}
    sandbox |= {"autoAllowBashIfSandboxed": True, "filesystem": {"denyRead": [str(path) for path in hidden]}}
    sandbox["network"] = {"allowedDomains": ["pypi.org", "files.pythonhosted.org"]}
    deny = [f"{tool}({path}/**)" for path in hidden for tool in ("Read", "Grep", "Glob")]
    return json.dumps({"permissions": {"deny": deny}, "sandbox": sandbox})


def claude(workspace: Path, prompt: str, arm: str, args: argparse.Namespace, log: Path) -> dict:
    command = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", args.model]
    command += ["--effort", args.effort, "--permission-mode", "acceptEdits", "--setting-sources", "project"]
    hidden = [PROBLEMS, *(args.out / name for name in args.roots if name != log.parent.name)]
    command += ["--settings", settings(hidden), "--tools", TOOLS, "--strict-mcp-config", "--max-budget-usd", "15"]
    command += ["--allowedTools", "Bash"]  # every command still runs in the sandbox; -p can't ask about the rest
    if arm == "with":
        command += ["--plugin-dir", str(args.plugin.resolve())]
    with log.open("w") as out, suppress(subprocess.TimeoutExpired):  # a timed-out session is killed and scored
        subprocess.run(command, cwd=workspace, stdout=out, stderr=subprocess.STDOUT, timeout=3600, check=False)
    lines = [json.loads(line) for line in log.read_text().splitlines() if line.startswith("{")]
    result = next((line for line in reversed(lines) if line.get("type") == "result"), {})
    session = next((line["session_id"] for line in lines if "session_id" in line), None)
    for transcript in Path.home().glob(f".claude/projects/*/{session}.jsonl"):  # hook feedback is only kept here
        shutil.copy(transcript, log.with_suffix(".transcript.jsonl"))
    keep = ("subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd", "usage", "session_id")
    return {key: result.get(key) for key in keep}


def pytest(problem: str, n: int, *args: str) -> str:
    """pytest's output for a checkpoint's hidden test file; empty if the submission hangs."""
    tests = PROBLEMS / problem / "tests"
    command = ["uv", "run", "--no-project", "--python", "3.12", "--with", "pytest", "python", "-m", "pytest", "-q"]
    command += [str(tests / f"test_checkpoint_{n}.py"), f"--checkpoint=checkpoint_{n}", "--tb=no", *args]
    with suppress(subprocess.TimeoutExpired):  # a hung submission passes nothing
        return subprocess.run(command, cwd=tests, capture_output=True, text=True, timeout=1800, check=False).stdout
    return ""


@cache
def test_count(problem: str, n: int) -> int:
    """Tests in a checkpoint's file, so one that fails to load or times out still counts in full."""
    found = re.search(r"(\d+) tests? collected", pytest(problem, n, "--entrypoint=none", "--collect-only"))
    return int(found[1]) if found else 0


def hidden_tests(copy: Path, python: str, problem: str, entry: str, checkpoint: int) -> dict[str, list[int]]:
    """[passed, total] per checkpoint's test file, run on the solution copy with a venv like Claude's."""
    subprocess.run(["uv", "venv", "-q", "--python", python], cwd=copy, check=False)
    if (copy / "requirements.txt").exists():
        subprocess.run(["uv", "pip", "install", "-q", "-r", "requirements.txt"], cwd=copy, check=False)
    found = {}
    for n in range(1, checkpoint + 1):
        output = pytest(problem, n, f"--entrypoint={copy}/.venv/bin/python {copy}/{entry}.py")
        passed = re.search(r"(\d+) passed", output)
        found[f"checkpoint_{n}"] = [int(passed[1]) if passed else 0, test_count(problem, n)]
    return found


def scb_check(copy: Path) -> dict:
    command = ["uvx", "--python", "3.12", SCB_CHECK, "check", str(copy), "--output-format", "json"]
    try:
        return json.loads(subprocess.run(command, capture_output=True, text=True, timeout=600, check=False).stdout)
    except (subprocess.TimeoutExpired, ValueError) as err:
        return {"error": str(err)[-500:]}


def run_chain(problem: str, arm: str, run: int, args: argparse.Namespace) -> None:
    root = args.out / f"{problem}-{arm}-{run}"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    for step in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "start"]):
        subprocess.run([*GIT, *step], cwd=workspace, check=True)
    (workspace / ".git" / "info" / "exclude").write_text(".venv/\n__pycache__/\n.pytest_cache/\n")
    entry = re.search(r"^entry_file: *(\S+)", (PROBLEMS / problem / "config.yaml").read_text(), re.MULTILINE)[1]
    for n in range(1, args.checkpoints + 1):
        spec = (PROBLEMS / problem / f"checkpoint_{n}.md").read_text()
        prompt = PROMPT.format(env=FIRST if n == 1 else LATER, spec=spec.strip())
        session = claude(workspace, prompt, arm, args, root / f"checkpoint_{n}.jsonl")
        for step in (["add", "-A"], ["commit", "-q", "--allow-empty", "-m", f"checkpoint {n}"], ["tag", f"cp{n}"]):
            subprocess.run([*GIT, *step], cwd=workspace, check=True)
        row = {"problem": problem, "arm": arm, "run": run, "checkpoint": n, "session": session}
        cfg = workspace / ".venv" / "pyvenv.cfg"  # test with the Python Claude built its venv with
        version = re.search(r"^version(?:_info)? = (\d+\.\d+)", cfg.read_text(), re.MULTILINE) if cfg.exists() else None
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "solution"
            shutil.copytree(workspace, copy, ignore=SOLUTION_ONLY)
            row["scb"] = scb_check(copy)  # before the test venv appears in the copy
            row["tests"] = hidden_tests(copy, version[1] if version else "3.12", problem, entry, n)
        with (args.out / "results.jsonl").open("a") as out:  # one short append per row: safe across threads
            out.write(json.dumps(row) + "\n")
        print(f"{problem} {arm} run {run} checkpoint {n}: tests {row['tests'][f'checkpoint_{n}']}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--problems", nargs="+", default=["etl_pipeline", "code_search"])
    parser.add_argument("--arms", nargs="+", default=["without", "with"], choices=["without", "with"])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--checkpoints", type=int, default=5)
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--plugin", type=Path, default=PLUGIN, help="plainpy checkout for the with arm")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    parser.add_argument("--out", type=Path, default=Path.home() / ".cache" / "plainpy-evals" / stamp)
    args = parser.parse_args()
    chains = [(p, a, r) for p in args.problems for r in range(1, args.runs + 1) for a in args.arms]
    args.roots = [f"{p}-{a}-{r}" for p, a, r in chains]
    for name in args.roots:  # all up front, so every session's sandbox can deny reads of the others
        (args.out / name).mkdir(parents=True)
    with ThreadPoolExecutor(args.jobs) as pool:
        list(pool.map(lambda chain: run_chain(*chain, args), chains))  # list() re-raises a chain's error
    print(f"results: {args.out}")


if __name__ == "__main__":
    main()
