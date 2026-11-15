"""Full class names for slopcheck, so two classes that share a short name are never mixed up.

A class's id is its module and its `__qualname__`: `pkg.a.Outer.Inner`. Base classes are
followed through imports and re-exports, and a module can be named by any unique tail of its
path (`pkg.a` for `src/pkg/a.py`). A base that can't be followed keeps its short name, which
stands for every project class of that name: a rule may then miss a finding, never report a wrong one.
"""

from __future__ import annotations

import ast
from collections import Counter, defaultdict
from pathlib import Path
from typing import cast

Function = ast.FunctionDef | ast.AsyncFunctionDef
MAX_HOPS = 20  # re-exports never chain this far; it also ends import cycles
BLOCKS = ("body", "orelse", "finalbody", "handlers", "cases")  # the fields that hold statements


def dotted(*parts: str | None) -> str:
    return ".".join(filter(None, parts))


def name_of(node: ast.expr) -> str:
    """`Protocol` for `Protocol`, `typing.Protocol` and `Protocol[T]`."""
    node = node.value if isinstance(node, ast.Subscript) else node
    return node.attr if isinstance(node, ast.Attribute) else path_of(node)


def path_of(node: ast.expr) -> str:
    """`a.b.C` for `a.b.C` and `a.b.C[T]`, and "" for anything else; a loop, as a long chain would overflow recursion."""
    node = node.value if isinstance(node, ast.Subscript) else node
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        node, parts = node.value, [node.attr, *parts]
    return dotted(node.id, *parts) if isinstance(node, ast.Name) else ""


def qualnames(tree: ast.Module) -> dict[ast.AST, str]:
    """`__qualname__` of each class and function (`Outer.Inner`, `make.<locals>.Impl`) and the
    scope of each import, in source order. Only statements are walked: these are never expressions."""
    names: dict[ast.AST, str] = {}
    todo: list[tuple[ast.AST, str]] = [(tree, "")]
    while todo:
        node, prefix = todo.pop()
        if isinstance(node, ast.ClassDef | Function):
            names[node] = prefix + node.name
            prefix = names[node] + (".<locals>." if isinstance(node, Function) else ".")
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names[node] = prefix
        children = (child for f in reversed(BLOCKS) for child in reversed(cast("list[ast.AST]", getattr(node, f, []))))
        todo += [(child, prefix) for child in children]
    return names


def class_defs(names: dict[ast.AST, str]) -> list[ast.ClassDef]:
    """The classes among `qualnames`: every class at any depth, in source order."""
    return [node for node in names if isinstance(node, ast.ClassDef)]


def module_of(path: str) -> str:
    """`pkg.a` for `pkg/a.py`, `pkg` for `pkg/__init__.py` (but `__init__` at the root)."""
    parts = Path(path).with_suffix("").parts
    return dotted(*(parts[:-1] if parts[-1] == "__init__" and len(parts) > 1 else parts))


def imported(path: str, names: dict[ast.AST, str]) -> list[tuple[str, str]]:
    """Each imported name, under its scope, and what it stands for: `("f.<locals>.Base", "pkg.a.Store")`.
    `import a.b` needs no entry, since `a.b.X` already names `X` in full."""
    package = Path(path).parent.parts
    bound: list[tuple[str, str]] = []
    for node, scope in names.items():
        if isinstance(node, ast.Import):
            bound += [(scope + a.asname, a.name) for a in node.names if a.asname]
        elif isinstance(node, ast.ImportFrom):
            source = package[: max(0, len(package) + 1 - node.level)] if node.level else ()
            bound += [(scope + (a.asname or a.name), dotted(*source, node.module, a.name)) for a in node.names]
    return bound


def resolve(name: str, table: dict[str, str]) -> str | None:
    """The id of the project class a full dotted name names. `table` maps each class id and
    module to itself, and each import and unique module tail to what it stands for."""
    for _ in range(MAX_HOPS):
        parts = name.split(".")
        head = next((dotted(*parts[:cut]) for cut in range(len(parts), 0, -1) if dotted(*parts[:cut]) in table), "")
        if not head or table[head] == head:
            return head if head == name else None
        name = dotted(table[head], name[len(head) + 1 :])
    return None


def ancestors(class_id: str, parents: dict[str, set[str]]) -> set[str]:
    """Every base above a class, through any number of levels."""
    seen: set[str] = set()
    todo = [class_id]
    while todo:
        for base in parents.get(todo.pop(), set()) - seen:
            seen.add(base)
            todo.append(base)
    return seen


def hierarchy(names: dict[str, dict[ast.AST, str]]) -> tuple[dict[ast.ClassDef, str], dict[str, set[str]]]:
    """Each class's id, and its ancestors: ids, and the short names of bases that couldn't be
    followed. `names` holds each file's `qualnames`."""
    modules = {path: module_of(path) for path in names}
    classes = [(cls, modules[path], names[path][cls]) for path in names for cls in class_defs(names[path])]
    ids = {cls: dotted(module, q) for cls, module, q in classes}
    known = set(ids.values())
    tails = [(dotted(*m.split(".")[cut:]), m) for m in set(modules.values()) for cut in range(1, m.count(".") + 1)]
    unique = Counter(tail for tail, _ in tails)
    ambiguous = {m for m in modules.values() if unique[m]}  # another module's path also ends in its name
    table = {tail: m for tail, m in tails if unique[tail] == 1 and tail not in modules.values()}
    bindings = [(dotted(modules[path], k), v) for path in names for k, v in imported(path, names[path])]
    bound = Counter(k for k, _ in set(bindings))
    table |= {k: v for k, v in bindings if bound[k] == 1}  # a name imported from two places stays unresolved
    table |= {name: name for name in [*modules.values(), *known]}  # a module wins over a name re-exported as it
    parents: defaultdict[str, set[str]] = defaultdict(set)
    for cls, module, q in classes:
        parents[cls.name].add(ids[cls])  # an unfollowed base's short name stands for every class of that name
        enclosing = q.split(".")[:-1]  # Python looks in its own scope, enclosing functions, the module
        cuts = [c for c in range(len(enclosing), -1, -1) if c in (0, len(enclosing)) or enclosing[c - 1] == "<locals>"]
        others = tuple(f"{m}." for m in ambiguous - {module})
        for base in cls.bases:
            candidates = [dotted(module, *enclosing[:c], name := path_of(base)) for c in cuts] + [name]
            found = [r for n in candidates if (r := resolve(n, table)) in known and not r.startswith(others)]
            parents[ids[cls]].add(next((r for r in found if r != ids[cls]), name_of(base)))  # never the class itself
    return ids, {class_id: ancestors(class_id, parents) - {"", "object"} for class_id in ids.values()}
