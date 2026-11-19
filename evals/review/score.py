"""Score the blind reviews against rubric.md's decision rule: pairwise wins, findings per 1,000 lines, reviewer checks.

Reads the reviews review.py made for $REVIEW (`review` by default)."""

import json
import os
from collections import defaultdict
from pathlib import Path
from statistics import mean

EVALS = Path.home() / ".cache" / "plainpy-evals"
NAME = os.environ.get("REVIEW", "review")
KEY = json.loads((EVALS / f"{NAME}-key.json").read_text())
RESULTS = EVALS / f"{NAME}-results"
NAMES = {"1": "correctness", "2": "error handling", "3": "readability", "4": "changeability"}
NAMES |= {"5": "needless structure", "6": "contortion", "7": "overall"}


def arm(sid: str) -> str:
    return KEY[sid].rsplit("-", 2)[1]


def problem(sid: str) -> str:
    return KEY[sid].rsplit("-", 2)[0]


def load(name: str) -> dict | None:
    path = RESULTS / f"{name}.json"
    return json.loads(path.read_text())["review"] if path.exists() else None


def winner(review: dict, dim: str, a: str, b: str) -> str:
    entry = review.get(dim)
    pick = str(entry.get("winner", "tie") if isinstance(entry, dict) else entry).strip().upper()
    return arm(a) if pick == "A" else arm(b) if pick == "B" else "tie"


def pairs() -> None:
    wins, flips, judged = defaultdict(float), 0, 0
    for w in sorted(KEY):
        for o in sorted(KEY):
            if arm(w) != "with" or arm(o) != "without" or problem(w) != problem(o):
                continue
            first, second = load(f"pair_{w}_{o}"), load(f"pair_{o}_{w}")
            if not first or not second:
                print(f"missing pair {w} {o}")
                continue
            for dim in NAMES:
                one, two = winner(first, dim, w, o), winner(second, dim, o, w)
                judged += 1
                flips += one != two
                agreed = one if one == two else "tie"
                wins[problem(w), dim] += 1 if agreed == "with" else 0.5 if agreed == "tie" else 0
                wins[problem(w), dim, "n"] += 1
    problems = sorted({p for p, *_ in wins})
    print("\nPairwise: plainpy's score out of the pairs judged (win 1, tie or split 0.5)")
    print("| dimension | " + " | ".join(problems) + " |\n" + "|---" * (len(problems) + 1) + "|")
    for dim, label in NAMES.items():
        cells = [f"{wins[p, dim]:g} / {wins[p, dim, 'n']:g}" for p in problems]
        print(f"| {dim} {label} | " + " | ".join(cells) + " |")
    print(
        f"\nOrder effect: {flips} of {judged} judgments ({flips / max(judged, 1):.0%}) change winner when A and B swap"
    )


def average(values: list[float]) -> str:
    return f"{mean(values):.1f}" if values else "n/a"


def lines(sid: str) -> int:
    files = (EVALS / NAME / "solutions" / sid).rglob("*.py")
    return sum(1 for path in files for line in path.read_text().splitlines() if line.strip())


def singles() -> None:
    found, scores, size = defaultdict(float), defaultdict(list), defaultdict(int)
    for sid in sorted(KEY):
        review = load(f"single_{sid}")
        if not review:
            print(f"missing single {sid}")
            continue
        group = problem(sid), arm(sid)
        size[group] += lines(sid)
        for finding in review.get("findings", []):
            if finding.get("severity") in ("high", "medium"):
                found[group, str(finding.get("dimension"))] += 1
        for dim, score in review.get("scores", {}).items():
            value = score.get("score") if isinstance(score, dict) else score
            if isinstance(value, int | float):
                scores[group, dim].append(value)
        print(f"{sid} {KEY[sid]}: provenance: {review.get('provenance')}")
    groups = sorted(size)
    print("\nHigh and medium findings per 1,000 non-blank lines, and mean score (1-5), by dimension")
    print("| dimension | " + " | ".join(f"{p} {a}" for p, a in groups) + " |\n" + "|---" * (len(groups) + 1) + "|")
    for dim in list(NAMES)[:6]:
        cells = [f"{found[g, dim] * 1000 / size[g]:.1f} · {average(scores[g, dim])}" for g in groups]
        print(f"| {dim} {NAMES[dim]} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    singles()
    pairs()
