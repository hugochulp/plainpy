"""Blinded copies of eval runs for review.py: checkpoint 5 code and specs only, under shuffled names.

Writes <evals>/<name>/solutions/S01... and specs/<problem>/, and the key (S01 -> run) to <evals>/<name>-key.json,
outside the folder the reviewers can read."""

import argparse
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from _findings import is_test_file

EVALS = Path.home() / ".cache" / "plainpy-evals"
PROBLEMS = Path(__file__).resolve().parents[1] / ".cache" / "scb-problems"


def copy_solution(workspace: Path, dest: Path) -> None:
    listing = ["git", "-C", str(workspace), "ls-tree", "-r", "--name-only", "cp5"]
    for name in subprocess.run(listing, capture_output=True, text=True, check=True).stdout.split():
        if (name.endswith(".py") or name == "requirements.txt") and not is_test_file(Path(name)):
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            show = ["git", "-C", str(workspace), "show", f"cp5:{name}"]
            target.write_bytes(subprocess.run(show, capture_output=True, check=True).stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", type=Path, help="run folders (<problem>-<arm>-<n>) made by run.py")
    parser.add_argument("--name", default="review", help="folder under ~/.cache/plainpy-evals")
    parser.add_argument("--seed", type=int, default=20261118)
    args = parser.parse_args()
    out = EVALS / args.name
    if out.exists() and not (out / "solutions").is_dir():
        sys.exit(f"{out} exists and is not a review set: pick another --name")
    shutil.rmtree(out, ignore_errors=True)
    runs = sorted(args.runs, key=lambda run: run.name)
    random.Random(args.seed).shuffle(runs)
    key = {}
    for n, run in enumerate(runs, 1):
        sid, problem = f"S{n:02d}", run.name.rsplit("-", 2)[0]
        key[sid] = run.name
        copy_solution(run / "workspace", out / "solutions" / sid)
        (out / "solutions" / sid / "PROBLEM").write_text(problem + "\n")
        (out / "specs" / problem).mkdir(parents=True, exist_ok=True)
        for spec in (PROBLEMS / problem).glob("checkpoint_*.md"):
            shutil.copy(spec, out / "specs" / problem / spec.name)
    # The same import order and formatting for every copy: plainpy fixes and formats every file it touches.
    ruff = ["uvx", "ruff@0.17.5"]
    subprocess.run([*ruff, "check", "-q", "--isolated", "--select", "I", "--fix", str(out / "solutions")], check=False)
    subprocess.run([*ruff, "format", "-q", "--isolated", str(out / "solutions")], check=True)
    (EVALS / f"{args.name}-key.json").write_text(json.dumps(key, indent=2) + "\n")
    print(f"{len(key)} solutions in {out}")


if __name__ == "__main__":
    main()
