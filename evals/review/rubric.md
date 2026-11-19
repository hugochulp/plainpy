# Blind code review: rubric and prompts (draft)

Does plainpy's code hold up under a careful human-style review, beyond the numbers it is built to improve?
Each final solution (its problem's last checkpoint) is reviewed alone, then compared in pairs, without the reviewer
knowing which run, arm or tool produced it.

## Material

- `~/.cache/plainpy-evals/review/solutions/S01`–`S12`: the last checkpoint's code of every run, code and
  `requirements.txt` only (no tests, venv, git history, transcripts). Names are shuffled (seed 20261118).
  Every copy gets the same Ruff import sort and format, since plainpy formats every file it touches and
  that alone would give its arm away, and suppression comments (`# noqa`, `# type: ignore`, `# slopcheck: allow`)
  are stripped, since only plainpy asks for them. `PROBLEM` names the problem.
- `~/.cache/plainpy-evals/review/specs/<problem>/checkpoint_1.md` onwards: the full spec the
  code was written against, in the order it arrived.
- The key (`S01` → run) is kept outside the review folder, and reviewers are denied reads of it.

Reviewers are fresh `claude -p` sessions (Opus 5.5, high effort) started in the review folder, with only
`Read`, `Grep` and `Glob`. Reads outside the folder need a permission nobody can give in `-p`, and the
key and the run folders are denied outright as well. They read the code; they don't run it. Correctness is
already measured by the hidden tests, which also serve as a check on the reviewers (below).

## Dimensions

| # | Dimension | What the reviewer judges | plainpy targets it? |
|---|---|---|---|
| 1 | Correctness | Does the code do what the spec says, including its edge cases and error rules? | no |
| 2 | Error handling | Bad input and failures handled as the spec asks; nothing swallowed, nothing crashes on input the spec allows | no |
| 3 | Readability | Can a new maintainer follow it: names, structure, control flow, comments where they earn their place | partly |
| 4 | Changeability | How hard would the next checkpoint be: duplication, coupling, how many places one rule lives in | partly |
| 5 | Needless structure | Classes, layers, indirection or options the problem doesn't need | **yes** |
| 6 | Metric-driven contortion | Code bent to stay under size or complexity limits: functions split at arbitrary points, logic hidden in dispatch tables or lambdas, parameter bags, flag arguments, helpers that only exist to move lines | no (its known risk) |
| 7 | Overall | Which would you rather inherit and extend? | — |

Dimensions 1, 2, 6 and 7 carry the verdict; 3 and 4 are reported but read with care; 5 is reported
only for completeness, since plainpy blocks most of it directly.

## Prompt: single review

> You are reviewing a Python program written against a specification that arrived in five parts.
> Read every file under `solutions/{sid}/` and every file under `specs/{problem}/`. Read nothing else.
>
> List the problems you find. For each: file and line, dimension (1–6 from the table below), severity,
> what is wrong, and for correctness or error handling the spec passage it breaks. Severity:
> **high** = wrong output, a crash or data loss on input the spec allows; **medium** = wrong on an edge
> case, poor failure behaviour, or a maintenance trap that would likely cause a bug; **low** = makes the
> code harder to read or change without being wrong.
>
> Report only problems you can point to in the code. Don't report style a formatter would settle, and
> don't pad the list: an empty dimension is a fine answer. Then give each dimension a score from 1
> (poor) to 5 (excellent) with one sentence of reason.
>
> {dimension table}
>
> Last, in one sentence: can you tell anything about how this code was produced? Say "no" if not.
>
> Reply with JSON only: `{"findings": [{"file", "line", "dimension", "severity", "problem", "spec"}],
> "scores": {"1": {"score", "reason"}, …, "6": {…}}, "provenance": "…"}`.

## Prompt: pairwise

> Two Python programs, A and B, were written against the same five-part specification. Read every file
> under `solutions/{a}/` (program A), `solutions/{b}/` (program B) and `specs/{problem}/`. Read nothing
> else.
>
> For each dimension below, say which program is better, or `tie` if neither is clearly better, with
> the one or two concrete differences that decided it (file and line). Length is not a merit or a fault
> in itself: judge what the code does and how easily it can be followed and changed.
>
> {dimension table, including 7}
>
> Reply with JSON only: `{"1": {"winner": "A" | "B" | "tie", "reason"}, …, "7": {…}}`.

## Runs

- 12 single reviews, one per solution.
- 18 pairs: every with-plainpy solution against every without-plainpy one of the same problem (3 × 3
  per problem). Each pair is judged twice, with A and B swapped: 36 judgments. A pair's result counts
  only where both orders agree; a disagreement counts as a tie.

## Decision rule (fixed before any review runs)

Per problem, on dimension 7 (overall), with ties counting half:

- **plainpy materially better:** it wins at least 7 of 9 pairs on both problems, and doesn't lose a
  majority of pairs on correctness (1) or error handling (2) on either problem.
- **plainpy materially worse:** the same with the arms swapped.
- **No material difference:** anything else.

Whatever the verdict, plainpy losing at least 6 of 9 pairs on metric-driven contortion (6) on either
problem is flagged as a problem in its own right.

Single reviews give the evidence behind the verdict: high and medium findings per 1,000 non-blank lines,
per dimension and arm, and their mean scores.

## Problem sets (fixed 2026-11-18, before round two ran)

The first two rounds tuned plainpy on `etl_pipeline` and `code_search` and judged it on the same two, so
their results can't show whether a change generalises. From here on:

| Set | Easy | Medium | Hard | Use |
|---|---|---|---|---|
| Development | `etl_pipeline`, `code_search` | `log_query`, `mvvault` | — | tuning; at most one more round after round two |
| Held-out | — | `layered_config_synthesizer`, `eve_jump_planner` | `meshctl`, `eve_industry` | run once, on the frozen plugin |

The new problems were drawn with `random.Random(20261120)` (4 Medium, then 2 Hard, with `sample`) from
SlopCodeBench's problems outside its web and networking categories, which the sandboxed harness may not be
able to serve. An earlier draw (seed 20261119, 2 Easy and 2 Medium held-out) was replaced before anything
ran, when the sets became 4 problems each. Nobody reads a held-out spec before the held-out run. Every change
made during development must follow a general principle, never a fix for one problem's bug.

**Contortion is judged by the single reviews.** The arm without plainpy has no limits, so it never shows
contortion and wins every pair on that dimension whenever plainpy shows any split; long functions count
against readability instead. Pairwise contortion is still reported.

**Held-out bar.** plainpy is materially better if, on at least 3 of the 4 held-out problems, it wins at
least 7 of 9 pairs overall (7) and its mean single-review contortion score (6) is at least 4 of 5; and it
loses a majority of pairs on correctness (1) or error handling (2) on at most one problem; and the hidden
tests passed don't drop. Materially worse is the same with the arms swapped; anything else is no material
difference.

## Checking the reviewers

- **Against the tests:** the hidden tests each solution fails are rerun and listed. A reviewer whose
  correctness findings match the failing tests earns trust in its other findings; one that misses them
  all, or flags passing behaviour as broken, doesn't.
- **Order effect:** the share of pairs where swapping A and B flips the winner. Above a third, the
  pairwise results are too noisy to support a verdict.
- **Blinding:** each single review ends by asking whether the reviewer can tell how the code was
  produced; any answer that names a tool or an arm is reported. The question comes after the review,
  so it can't steer it.
- **One reviewer per solution:** a single review is one sample. If two reviews of the same solution
  would disagree a lot, the findings counts are only a rough guide; the pairwise verdict, judged twice,
  is the sturdier result.
