"""Blind reviews of the eval solutions, one fresh read-only `claude -p` session each (see rubric.md).

Reads the blinded copies in <evals>/<name> (made by blind.py) and the key beside it; writes each review to
<evals>/<name>-results. The name is $REVIEW, `review` by default."""

import argparse
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from itertools import product
from pathlib import Path

EVALS = Path.home() / ".cache" / "plainpy-evals"
NAME = os.environ.get("REVIEW", "review")
REVIEW, KEY, RESULTS = EVALS / NAME, EVALS / f"{NAME}-key.json", EVALS / f"{NAME}-results"
DIMENSIONS = """1. Correctness: does the code do what the spec says, including its edge cases and error rules?
2. Error handling: bad input and failures handled as the spec asks; nothing swallowed, no crash on input the spec allows.
3. Readability: can a new maintainer follow it (names, structure, control flow, comments where they earn their place)?
4. Changeability: how hard would the next part of the spec be (duplication, coupling, how many places one rule lives in)?
5. Needless structure: classes, layers, indirection or options the problem doesn't need.
6. Metric-driven contortion: code bent to stay under size or complexity limits (functions split at arbitrary points, logic
   hidden in dispatch tables or lambdas, parameter bags, flag arguments, helpers that only exist to move lines)."""
OVERALL = "7. Overall: which would you rather inherit and extend?"
SINGLE = f"""You are reviewing a Python program written against a specification that arrived in five parts.
Read every file under `solutions/{{sid}}/` and every file under `specs/{{problem}}/`. Read nothing else.

List the problems you find. For each: file and line, dimension (1-6 below), severity, what is wrong, and for correctness
or error handling the spec passage it breaks. Severity: high = wrong output, a crash or data loss on input the spec
allows; medium = wrong on an edge case, poor failure behaviour, or a maintenance trap that would likely cause a bug;
low = makes the code harder to read or change without being wrong.

Report only problems you can point to in the code. Don't report style a formatter would settle, and don't pad the list:
an empty dimension is a fine answer. Then give each dimension a score from 1 (poor) to 5 (excellent) with one sentence
of reason.

{DIMENSIONS}

Last, in one sentence: can you tell anything about how this code was produced? Say "no" if not.

Reply with JSON only: {{{{"findings": [{{{{"file", "line", "dimension", "severity", "problem", "spec"}}}}],
"scores": {{{{"1": {{{{"score", "reason"}}}}, ..., "6": {{{{...}}}}}}}}, "provenance": "..."}}}}"""
PAIR = f"""Two Python programs, A and B, were written against the same five-part specification. Read every file under
`solutions/{{a}}/` (program A), `solutions/{{b}}/` (program B) and `specs/{{problem}}/`. Read nothing else.

For each dimension below, say which program is better, or "tie" if neither is clearly better, with the one or two
concrete differences that decided it (file and line). Length is not a merit or a fault in itself: judge what the code
does and how easily it can be followed and changed.

{DIMENSIONS}
{OVERALL}

Reply with JSON only: {{{{"1": {{{{"winner": "A" | "B" | "tie", "reason"}}}}, ..., "7": {{{{...}}}}}}}}"""


def settings() -> str:
    """Only the review folder: the key, the runs and other reviews are denied (reads outside cwd can't be asked in -p)."""
    hidden = [KEY, RESULTS, *(path for path in EVALS.iterdir() if path != REVIEW), Path(__file__).resolve().parents[2]]
    deny = [f"{tool}(/{path}{'/**' if path.is_dir() else ''})" for path in hidden for tool in ("Read", "Grep", "Glob")]
    return json.dumps({"permissions": {"deny": deny}})


def review(name: str, prompt: str, args: argparse.Namespace) -> None:
    out = RESULTS / f"{name}.json"
    if out.exists():  # reruns pick up where a stopped run left off
        return
    command = ["claude", "-p", prompt, "--output-format", "json", "--model", args.model, "--effort", args.effort]
    command += ["--tools", "Read,Grep,Glob", "--strict-mcp-config", "--setting-sources", "project"]
    command += ["--settings", settings(), "--max-budget-usd", "10"]
    try:
        done = subprocess.run(command, cwd=REVIEW, capture_output=True, text=True, timeout=3600, check=False)
        session, stderr = json.loads(done.stdout or "{}"), done.stderr
    except (subprocess.TimeoutExpired, ValueError) as err:  # recorded as failed, so a rerun tries it again
        session, stderr = {"is_error": True, "subtype": type(err).__name__}, str(err)
    found = re.search(r"\{.*\}", session.get("result") or "", re.DOTALL)
    try:
        session["review"] = json.loads(found[0]) if found else None
    except ValueError:
        session["review"] = None
    if session.get("is_error") or session["review"] is None:
        print(f"{name}: no review ({session.get('subtype')}): {(session.get('result') or stderr)[:300]}", flush=True)
        (RESULTS / f"{name}.failed.json").write_text(json.dumps(session, indent=1))
        return
    out.write_text(json.dumps(session, indent=1))
    print(f"{name}: done, ${session.get('total_cost_usd', 0):.2f}", flush=True)


def jobs() -> list[tuple[str, str]]:
    key = json.loads(KEY.read_text())
    problem = {sid: run.rsplit("-", 2)[0] for sid, run in key.items()}
    found = [(f"single_{sid}", SINGLE.format(sid=sid, problem=problem[sid])) for sid in sorted(key)]
    arm = {sid: run.rsplit("-", 2)[1] for sid, run in key.items()}
    for w, o in product(sorted(key), sorted(key)):
        if arm[w] == "with" and arm[o] == "without" and problem[w] == problem[o]:
            for a, b in ((w, o), (o, w)):
                found.append((f"pair_{a}_{b}", PAIR.format(a=a, b=b, problem=problem[w])))
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", nargs="*", help="review names to run (default: all)")
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--jobs", type=int, default=3)
    args = parser.parse_args()
    RESULTS.mkdir(exist_ok=True)
    todo = [job for job in jobs() if not args.only or job[0] in args.only]
    with ThreadPoolExecutor(args.jobs) as pool:
        list(pool.map(lambda job: review(*job, args), todo))


if __name__ == "__main__":
    main()
