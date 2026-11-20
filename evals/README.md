# Evals

Does plainpy change how Claude's code holds up when it extends its own code, checkpoint after checkpoint?
The tasks, the prompt and the erosion/verbosity scores come from SlopCodeBench
([paper](https://arxiv.org/abs/2603.24755), [problems](https://github.com/gabeorlanski/scb-problems),
[scb-check](https://github.com/gabeorlanski/scb-check), all Apache-2.0).

Each run starts in an empty git repo. Every checkpoint is a fresh `claude -p` session on what the last one
left, run once without plainpy and once with it (via `--plugin-dir`). Bash runs in Claude
Code's sandbox (writes to the workspace, network to PyPI only); the hidden tests can't be read.

## Setup (once)

```bash
git clone -q --filter=blob:none --no-checkout https://github.com/gabeorlanski/scb-problems.git evals/.cache/scb-problems
```

```bash
git -C evals/.cache/scb-problems sparse-checkout set --no-cone /etl_pipeline/ /code_search/ '!/etl_pipeline/solutions/' '!/code_search/solutions/' && git -C evals/.cache/scb-problems checkout -q 9cd9ca3a51c3d3e2a99d2488a25baf73a2204451
```

## Run

```bash
python3 evals/run.py --problems etl_pipeline --checkpoints 2 --runs 1
```

```bash
python3 evals/measure.py ~/.cache/plainpy-evals/<timestamp>
```

Defaults: etl_pipeline and code_search, every checkpoint, 3 runs per arm, Opus 5.5 at medium effort, 2 runs at a time.
`--plugin <dir>` tests another plainpy checkout in the with arm (default: this one). For a real round, copy
the plugin somewhere first (`evals/.cache/plainpy-<name>`), so edits made while it runs don't change what's tested.
Runs use your own Claude login and count against its usage.

## Blind review

```bash
python3 evals/review/blind.py ~/.cache/plainpy-evals/<timestamp>/*-*-* --name <set>
```

```bash
cd evals/review && REVIEW=<set> python3 review.py --jobs 4 && REVIEW=<set> python3 score.py
```

`blind.py` copies each run's last checkpoint under a shuffled name, formatted alike and with suppression comments
stripped. `review.py` has read-only Opus sessions write one review per solution and judge every with×without pair
in both orders. `score.py` applies the decision rule in `review/rubric.md`, where each round's plan and results are
recorded.
