---
name: simple-python
description: How to write and refactor Python in a plainpy project - when a class, dataclass or Pydantic model is justified, how to cut complexity without adding layers, and how to fix plainpy check failures. Use when writing, refactoring or fixing Python code, or when a plainpy check fails.
---

# Simple Python

Write the least code that solves the task, in the flattest shape that stays readable.
plainpy's checks enforce limits on size and complexity, but they cannot see over-abstraction,
so this guide covers what the tools miss.

## Choosing a shape: function, then dataclass, then class

1. **Function.** The default. Data in, data out.
2. **Dataclass** (`@dataclass(frozen=True)` where possible). A named group of fields
   that travels together through several functions.
3. **Class with methods.** Only when it holds state *and* behaviour that changes that state,
   or when there are two or more real implementations of the same interface today.

Not justified on their own: "it might be extended later", "it groups related functions"
(use a module), "it holds configuration" (pass arguments), "the linter said too many arguments".

## Pydantic only at the edges

Parse untrusted input into Pydantic models where it enters: HTTP bodies, files, environment,
CLI arguments, third-party API responses. After that, pass plain values or dataclasses.
Do not create a model for data that never crosses a boundary, and do not re-validate data
that a model or type already guarantees.

## Avoid speculative generality

- No ABC or Protocol with a single implementation.
- No parameters, flags or hooks that no caller uses.
- No `utils.py` / `helpers.py` grab-bags. Put a function next to its only caller.
- No new dependency for something the stdlib does in a few lines.
- No wrapper whose body only forwards to another function.

## When a plainpy check fails

The hook prints the file, line, rule and limit. Fix the cause:

| Failure | Do | Do not |
|---|---|---|
| Cognitive complexity (CPX001), C901, too many branches or returns | Early returns and guard clauses; replace `if/elif` chains on a value with a dict lookup; merge duplicate branches; drop cases the task does not need | Split into single-use helpers, add a strategy class, or hide branches in lambdas |
| Too many arguments (PLR0913/PLR0917) | Remove parameters callers never vary; make the rest keyword-only | Bundle them into a new config class |
| Too many statements (PLR0915) | Remove what the task does not need, then extract a step only if it has a name and a second use | Chop the function into numbered steps |
| Any rule | Change the code | Add `# noqa`, `# type: ignore`, or edit lint config |

Only code you changed is judged. Do not "fix" unrelated existing code unless asked.

## Suppressions, config and dependencies

plainpy refuses new suppression comments that don't name the rule and give a reason.
File-wide suppressions (`# ruff: noqa`, `# mypy: ignore-errors`, ...) are always refused.
When a suppression really is the right call, which is rare, write it like this:

```python
result = legacy_api(payload)  # type: ignore[no-untyped-call] -- vendor SDK ships without stubs
```

Changes to lint or type-checker config (`[tool.ruff]`, `ruff.toml`, `.plainpy.toml`, ...)
and new runtime dependencies go to the user for approval. Don't try to route around this
through the shell; ask the user, and explain why the change is needed.

## Examples

Branching on a value becomes a lookup:

```python
# Before: complexity grows with every case
def unit_price(tier: str) -> int:
    if tier == "free":
        return 0
    elif tier == "pro":
        return 20
    elif tier == "team":
        return 15
    raise ValueError(tier)

# After
UNIT_PRICES = {"free": 0, "pro": 20, "team": 15}

def unit_price(tier: str) -> int:
    return UNIT_PRICES[tier]
```

A one-method class becomes a function:

```python
# Before
class ReportBuilder:
    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows

    def build(self) -> str:
        return "\n".join(format_row(r) for r in self.rows)

# After
def build_report(rows: list[Row]) -> str:
    return "\n".join(format_row(r) for r in rows)
```

Nesting becomes guard clauses:

```python
# Before
def ship(order: Order) -> None:
    if order.paid:
        if order.items:
            if not order.shipped:
                dispatch(order)

# After
def ship(order: Order) -> None:
    if not order.paid or not order.items or order.shipped:
        return
    dispatch(order)
```
