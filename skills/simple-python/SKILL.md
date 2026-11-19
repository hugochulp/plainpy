---
name: simple-python
description: How to write and refactor Python in a plainpy project - when a class, dataclass or Pydantic model is justified, how to cut complexity without adding layers, and how to fix plainpy check failures. Use when writing, refactoring or fixing Python code, or when a plainpy check fails.
---

# Simple Python

Write the least structure that solves the whole task, in the flattest shape that stays readable.
Small means fewer layers, not fewer features: implement everything the task asks, edge cases included.
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

Input from outside can be malformed in ways the task doesn't list (truncated, the wrong type,
out of range). Where it enters, turn such failures into the program's own error format rather
than a traceback.

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
| Cognitive complexity (CPX001), C901 | Early returns and guard clauses; merge duplicate branches; turn an `if/elif` chain that only picks a value into a dict of values; move a step that does real work into a named function, even with one caller, where you can describe it without saying "part of". If the plain code is still over the limit, keep it whole, with the comment on its `def` line: `# noqa: C901 -- why the logic needs these branches` (CPX001: `# noqa: complexipy -- why`) | Cut a function at an arbitrary point into fragments; drop error handling the input can trigger; hide branches in lambdas or dispatch tables; add flag arguments; pack conditions into one line; add a strategy class |
| Too many arguments (PLR0913/PLR0917) | Remove parameters callers never vary; make the rest keyword-only | Bundle them into a new config class |
| Too many statements (PLR0915) | Remove what the task does not need, then move a step that has a name of its own into a function | Chop the function into numbered steps |
| Type error (basedpyright) | Fix the types at their source | Add `Any`, `cast()` or `# type: ignore` |
| Unused code (vulture) | Delete it, or ask the user to whitelist it if it's used dynamically | Add a fake caller to keep it alive |
| One-method class (SLP002) | Turn it into a function; static-method-only classes become module functions | Add a second method to pass the check |
| Interface with one implementation (SLP001) | Use the class directly; add an ABC or Protocol when a second implementation exists | Add a fake second implementation |
| Pass-through wrapper (SLP004) | Call the wrapped function directly and delete the wrapper | Add a trivial step to the wrapper |
| Single-use tiny helper (SLP005) | Inline it at its one call site | Call it twice to pass the check |
| Class budget (SLP006) | Merge classes, or use functions and dataclasses; if each class is needed, tell the user why | Turn classes into dicts of lambdas |
| Pydantic model away from I/O (SLP003) | Use a dataclass or plain values; if it mirrors a wire format plainpy can't see, add `# slopcheck: allow SLP003 -- reason` | Add a pointless `model_dump()` call |
| Unused option (SLP007) | Drop the parameter and use its default value inline | Pass the default at a call site to keep it |
| Placeholder code (SLP008) | Write the real body, or delete the function and its callers' expectations | Return a dummy value or log a TODO |
| Defensive check (SLP009) | Delete the `isinstance` or `None` check the types already guarantee | Widen the parameter's type to justify the check |
| Wide module or grab-bag name (SLP010) | Move functions next to their callers; make helpers private | Rename `utils.py` to `misc.py` |
| Too many exception classes (SLP011) | Raise built-in exceptions (`ValueError`, `KeyError`) with a clear message; add a class only when a caller catches it | Nest them in one module to look smaller |
| Lambdas in a table (SLP012) | Write the cases out as `if/elif` or `match`. A dict of named functions is fine only when each is a real step with a name of its own; for operators, use the `operator` module | Turn each lambda into a one-line `def` to pass the check |
| Schema with several models over the class budget (SLP006) | If every model is part of the schema, tell the user, or allow it: `# slopcheck: allow SLP006 -- part of the API schema` | Flatten the schema into dicts |
| Duplicated code (R0801) | Keep one copy, next to its callers | Move it into a new `utils.py` |
| Failing test | Fix the code, or the test if the test is wrong and the user agrees | Delete, skip or weaken the test |
| Any other rule | Change the code; if it is right as it is, suppress that one reported finding with a reason | Suppress a rule nothing reported (RUF100 flags it), or edit lint config |

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

Branching that only picks a value becomes a lookup (values, not code: a table of lambdas hides the
branches from the complexity checks instead of removing them):

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
