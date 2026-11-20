"""Report a run.py output folder: each metric per problem, arm and checkpoint, as median [min-max] over runs.

scb-check gives SlopCodeBench's erosion and verbosity; this adds what plainpy's rules are about (classes,
class-like workarounds, tiny functions, suppressions), how much earlier code each checkpoint rewrote, the
hidden-test pass rates and the session cost. The slope table fits a line through each run's checkpoints.
"""

import ast
import json
import math
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from _findings import is_test_file
from guard_edit import added_suppressions  # scripts/ is not a package: it goes on the path first

RULE = re.compile(r"\.py(?::\d+)? ([A-Za-z]\w+)\b")  # "app.py:12 C901 ..." or "app.py reportX x2 ..."
CLASS_LIKE = {"namedtuple", "NamedTuple", "TypedDict", "type", "make_dataclass"}


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, check=True).stdout


def class_like(node: ast.AST) -> bool:
    """namedtuple(...), TypedDict(...), type(name, bases, attrs) and friends: a class without `class`."""
    func = getattr(node, "func", None)  # only calls have one
    name = getattr(func, "id", None) or getattr(func, "attr", None)
    return name in CLASS_LIKE and (name != "type" or len(node.args) == 3)


def solution(name: str) -> bool:
    """A .py file of the solution, not a test Claude wrote for itself (run.py scores the same files)."""
    return name.endswith(".py") and not is_test_file(Path(name))


def shape(workspace: Path, commit: str) -> dict[str, int]:
    """Counts over the solution's files at a commit."""
    found = dict.fromkeys(("classes", "class_like", "functions", "tiny_functions", "suppressions"), 0)
    for name in git(workspace, "ls-tree", "-r", "--name-only", commit).split():
        if not solution(name):
            continue
        source = git(workspace, "show", f"{commit}:{name}")
        found["suppressions"] += len(added_suppressions("", source))  # comments only, as plainpy sees them
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            found["classes"] += isinstance(node, ast.ClassDef)
            found["class_like"] += class_like(node)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                found["functions"] += 1
                found["tiny_functions"] += len(node.body) < 3 and not node.name.startswith("__")
    return found


def churn(workspace: Path, commit: str) -> dict[str, int]:
    """Lines added, and lines deleted from .py files that existed before this checkpoint."""
    numstat = [line.split("\t") for line in git(workspace, "diff", "--numstat", f"{commit}~1", commit).splitlines()]
    stats = [columns for columns in numstat if solution(columns[2])]
    return {"added": sum(int(s[0]) for s in stats), "rewritten": sum(int(s[1]) for s in stats)}


def blocks(transcript: Path) -> tuple[dict[str, int], Counter[str]]:
    """Hook blocks Claude saw, per edit and at Stop, and the rule codes they named."""
    found, rules = {"edit_blocks": 0, "stop_blocks": 0}, Counter[str]()
    lines = transcript.read_text().splitlines() if transcript.exists() else []
    for line in lines:
        attachment = json.loads(line).get("attachment") or {}
        if attachment.get("type") == "hook_blocking_error":
            found["stop_blocks" if attachment.get("hookEvent") == "Stop" else "edit_blocks"] += 1
            rules.update(RULE.findall(attachment["blockingError"]["blockingError"]))
    return found, rules


def measure(out: Path, row: dict) -> tuple[dict[str, float], Counter[str]]:
    chain = out / f"{row['problem']}-{row['arm']}-{row['run']}"
    workspace = chain / "workspace"
    hooks, rules = blocks(chain / f"checkpoint_{row['checkpoint']}.transcript.jsonl")
    commit = f"cp{row['checkpoint']}"  # run.py tags each checkpoint's commit
    tests = row["tests"]
    current = tests[f"checkpoint_{row['checkpoint']}"]
    passed, total = (sum(column) for column in zip(*tests.values(), strict=True))
    session = row["session"]
    metrics = {
        "tests_now": current[0] / current[1] if current[1] else 0.0,
        "tests_so_far": passed / total if total else 0.0,
        "erosion": row["scb"].get("erosion", float("nan")),
        "verbosity": row["scb"].get("verbosity", float("nan")),
        "loc": row["scb"].get("total_loc", float("nan")),
        **shape(workspace, commit),
        **churn(workspace, commit),
        "cost_usd": session.get("total_cost_usd") or 0.0,
        "minutes": (session.get("duration_ms") or 0) / 60000,
        "turns": session.get("num_turns") or 0,
        **hooks,
    }
    return metrics, rules


def slope(points: list[tuple[int, float]]) -> float:
    xs, ys = zip(*points, strict=True)
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    spread = sum((x - mean_x) ** 2 for x in xs)
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / spread if spread else 0.0


def cell(values: list[float]) -> str:
    return f"{median(values):.3g} [{min(values):.3g}-{max(values):.3g}]" if values else "n/a"


def main() -> None:
    out = Path(sys.argv[1])
    table: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    runs: dict[tuple, dict[str, list[tuple[int, float]]]] = defaultdict(lambda: defaultdict(list))
    rules: dict[tuple, Counter[str]] = defaultdict(Counter)
    for row in map(json.loads, (out / "results.jsonl").read_text().splitlines()):
        metrics, named = measure(out, row)
        rules[row["problem"], row["arm"]].update(named)
        for name, value in metrics.items():
            if math.isnan(value):  # scb-check failed for this checkpoint
                continue
            table[row["problem"], row["arm"], row["checkpoint"]][name].append(value)
            runs[row["problem"], row["arm"], row["run"]][name].append((row["checkpoint"], value))
    slopes: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for (problem, arm, _), series in runs.items():
        for name, points in series.items():
            slopes[problem, arm][name].append(slope(points))
    names = list(dict.fromkeys(name for group in table.values() for name in group))  # one group may lack some
    for heading, groups in (("", table), ("\nSlope per checkpoint, median [min-max] over runs\n", slopes)):
        columns = ("problem", "arm", "cp")[: len(next(iter(groups)))]
        print(heading + "| " + " | ".join([*columns, *names]) + " |")
        print("|---" * (len(columns) + len(names)) + "|")
        for key in sorted(groups):
            print("| " + " | ".join(map(str, key)) + " | " + " | ".join(cell(groups[key][n]) for n in names) + " |")
    print("\nRules in hook blocks, summed over runs and checkpoints\n")
    for key in sorted(rules):
        print(f"- {' '.join(key)}: " + ", ".join(f"{code} {n}" for code, n in rules[key].most_common()))


if __name__ == "__main__":
    main()
