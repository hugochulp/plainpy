"""Blinded copies of eval runs for review.py: last-checkpoint code and specs only, under shuffled names.

Writes <evals>/<name>/solutions/S01... and specs/<problem>/, and the key (S01 -> run) to <evals>/<name>-key.json,
outside the folder the reviewers can read."""

import argparse
import io
import json
import random
import re
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))  # plainpy's scripts and evals: not packages
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _findings import is_test_file
from guard_edit import LINE_LEVEL
from measure import git
from problems import checkpoint_spec, last_checkpoint

EVALS = Path.home() / ".cache" / "plainpy-evals"
# Only plainpy asks for reasoned suppressions, so they would give the arm away.
SUPPRESSION = re.compile(rf"\s*{LINE_LEVEL.pattern}.*$", re.IGNORECASE)


def strip_suppressions(source: str) -> str:
    """Cut each comment from its first suppression marker on; strings that only look like one are left alone."""
    lines = source.splitlines(keepends=True)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and (found := SUPPRESSION.search(token.string)):
            row, col = token.start[0] - 1, token.start[1]
            kept = token.string[: found.start()].rstrip()
            lines[row] = lines[row][:col].rstrip() + (" " + kept if kept and col else kept) + "\n"
    return "".join(lines)


def copy_solution(workspace: Path, dest: Path, tag: str) -> None:
    for name in git(workspace, "ls-tree", "-r", "--name-only", tag).split():
        if (name.endswith(".py") or name == "requirements.txt") and not is_test_file(Path(name)):
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            text = git(workspace, "show", f"{tag}:{name}")
            target.write_text(strip_suppressions(text) if name.endswith(".py") else text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", type=Path, help="run folders (<problem>-<arm>-<n>) made by run.py")
    parser.add_argument("--name", default="review", help="folder under ~/.cache/plainpy-evals")
    parser.add_argument("--seed", type=int, default=20261118)
    args = parser.parse_args()
    out = EVALS / args.name
    if out.exists() and not (out / "solutions").is_dir():
        sys.exit(f"{out} exists and is not a review set: pick another --name")
    if (EVALS / f"{args.name}-results").exists():  # review.py would reuse them for different programs
        sys.exit(f"{args.name} has reviews already: pick another --name")
    shutil.rmtree(out, ignore_errors=True)
    runs = sorted(args.runs, key=lambda run: run.name)
    random.Random(args.seed).shuffle(runs)
    key = {}
    for n, run in enumerate(runs, 1):
        sid, problem = f"S{n:02d}", run.name.rsplit("-", 2)[0]
        key[sid] = run.name
        copy_solution(run / "workspace", out / "solutions" / sid, f"cp{last_checkpoint(problem)}")
        (out / "solutions" / sid / "PROBLEM").write_text(problem + "\n")
        specs = out / "specs" / problem
        if not specs.exists():  # once per problem, not per run
            specs.mkdir(parents=True)
            for checkpoint in range(1, last_checkpoint(problem) + 1):
                (specs / f"checkpoint_{checkpoint}.md").write_text(checkpoint_spec(problem, checkpoint) + "\n")
    # The same import order and formatting for every copy: plainpy fixes and formats every file it touches.
    ruff = ["uvx", "ruff@0.17.5"]
    subprocess.run([*ruff, "check", "-q", "--isolated", "--select", "I", "--fix", str(out / "solutions")], check=False)
    subprocess.run([*ruff, "format", "-q", "--isolated", str(out / "solutions")], check=True)
    (EVALS / f"{args.name}-key.json").write_text(json.dumps(key, indent=2) + "\n")
    print(f"{len(key)} solutions in {out}")


if __name__ == "__main__":
    main()
