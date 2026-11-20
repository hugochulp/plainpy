"""Single-file rules that check_file runs after every edit: SLP002, SLP004 and SLP012."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from _findings import Finding, is_test_file

from .classes import Function, class_defs, name_of, qualnames
from .shared import base_names, functions, is_dunder, methods, parse, placed, statements


def class_as_function(cls: ast.ClassDef) -> str | None:
    """SLP002: a class whose only job one function could do."""
    own = methods(cls)
    if cls.bases or cls.keywords or cls.decorator_list or not own:
        return None
    if all(any(name_of(d) == "staticmethod" for d in m.decorator_list) for m in own):
        return f"`{cls.name}` only has static methods: make them module-level functions"
    public = [m for m in own if not m.name.startswith("_") or m.name == "__call__"]
    other_dunders = [m for m in own if is_dunder(m.name) and m.name not in {"__init__", "__call__"}]
    if len(public) != 1 or other_dunders:
        return None
    what = " that takes what `__init__` stores" if any(m.name == "__init__" for m in own) else ""
    return f"`{cls.name}` has one public method, `{public[0].name}`: make it a function{what}"


def forwarded(call: ast.Call) -> list[tuple[str, bool]] | None:
    """(name, passed by position) for each argument, if every one is a bare parameter name.
    Unpacked arguments keep their `*` or `**`, so packing or unpacking never matches."""
    names = []
    for arg in call.args:
        starred = isinstance(arg, ast.Starred)
        value = arg.value if starred else arg
        if not isinstance(value, ast.Name):
            return None
        names.append(("*" * starred + value.id, True))
    for keyword in call.keywords:
        if not isinstance(keyword.value, ast.Name) or keyword.arg not in {None, keyword.value.id}:
            return None
        names.append(("**" * (keyword.arg is None) + keyword.value.id, False))
    return names


def pass_through(function: Function, *, method: bool) -> str | None:
    """SLP004: a function that only hands its arguments, unchanged, to another call."""
    body = statements(function)
    if function.decorator_list or is_dunder(function.name) or len(body) != 1:
        return None
    value = body[0].value if isinstance(body[0], ast.Return | ast.Expr) else None
    if isinstance(value, ast.Await):
        value = value.value
    if not isinstance(value, ast.Call):
        return None
    arguments = function.args
    positional = [a.arg for a in [*arguments.posonlyargs, *arguments.args]]
    positional = positional[method and positional[:1] == ["self"] :]
    params = [*positional, *(a.arg for a in arguments.kwonlyargs)]
    params += [prefix + a.arg for prefix, a in (("*", arguments.vararg), ("**", arguments.kwarg)) if a]
    names = forwarded(value)
    if not params or names is None or sorted(name for name, _ in names) != sorted(params):
        return None
    in_order = [name for name, by_position in names if by_position and name in positional]
    if in_order != sorted(in_order, key=positional.index):
        return None  # reordered arguments: an adapter, not a pass-through
    callee = ast.unparse(value.func)
    return f"`{function.name}` only passes its arguments on to `{callee}`: call `{callee}` directly"


TABLES = ast.Dict | ast.List | ast.Tuple | ast.Set
Definition = ast.ClassDef | Function
LOGIC = ast.IfExp | ast.BoolOp | ast.Compare | ast.BinOp | ast.UnaryOp  # what a lambda's branches are made of


def is_logic(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant):
        return False  # a signed number, as in `row[-1]`
    return isinstance(node, LOGIC) or (isinstance(node, ast.Call) and name_of(node.func) == "isinstance")


def has_logic(function: ast.Lambda) -> bool:
    """`lambda a, b: a < b`, `lambda s: s or None` and `lambda v: isinstance(v, str)` have it (a branch's
    condition or arms); `lambda row: row.name.lower()`, `lambda row: row[-1]` and `lambda: []` don't."""
    return any(map(is_logic, ast.walk(function.body)))


def logic_lambdas(table: TABLES) -> int:
    """Lambdas with logic in a table, counted through nested tables (a list of `(name, lambda)` pairs)."""
    members = table.values if isinstance(table, ast.Dict) else table.elts
    found = 0
    for member in members:
        found += isinstance(member, ast.Lambda) and has_logic(member)
        found += logic_lambdas(member) if isinstance(member, TABLES) else 0
    return found


def outermost_tables(tree: ast.Module) -> Iterator[tuple[TABLES, Definition | None, str]]:
    """Each table not inside another, with its enclosing class or function (None at module level)
    and, at module level, the variable it is assigned to."""
    todo: list[tuple[ast.AST, ast.AST | None, Definition | None, str]] = [(tree, None, None, "<module>")]
    while todo:
        node, parent, owner, label = todo.pop()
        if isinstance(node, TABLES) and not isinstance(parent, TABLES):
            yield node, owner, label
        if isinstance(node, Definition):
            owner = node
        elif owner is None and isinstance(node, ast.stmt):
            label = module_label(node, label)
        todo += [(child, node, owner, label) for child in ast.iter_child_nodes(node)]


def module_label(statement: ast.stmt, label: str) -> str:
    """What a module-level table is known by: the variable it is assigned to, or the call it is passed to."""
    if isinstance(statement, ast.Assign):
        return ast.unparse(statement.targets[0])
    if isinstance(statement, ast.AnnAssign | ast.AugAssign):
        return ast.unparse(statement.target)
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
        return ast.unparse(statement.value.func) + "()"
    return label


def lambda_tables(tree: ast.Module, names: dict[ast.AST, str]) -> list[Finding]:
    """SLP012: two or more lambdas with logic in one table, where the complexity checks can't see their branches.
    The finding sits on the enclosing definition, or on the table itself at module level."""
    return [
        Finding(
            "SLP012",
            (owner or table).lineno,
            f"{count} lambdas with logic in the table at line {table.lineno}",
            names[owner] if owner else label,
            "",
        )
        for table, owner, label in outermost_tables(tree)
        if (count := logic_lambdas(table)) >= 2
    ]


def file_findings(source: str, path: str) -> list[Finding]:
    """Single-file rules for one file: SLP002, SLP004 and SLP012."""
    tree = None if is_test_file(Path(path)) or path.endswith(".pyi") else parse(source)
    if tree is None:
        return []
    names = qualnames(tree)
    classes = class_defs(names)
    standalone = [cls for cls in classes if not base_names(cls)]  # a subclass's methods may be called by its base
    defs = functions(tree, standalone, names)
    found = [
        Finding("SLP002", cls.lineno, message, names[cls], "") for cls in classes if (message := class_as_function(cls))
    ]
    found += [
        Finding("SLP004", function.lineno, message, scope, "")
        for function, scope in defs
        if (message := pass_through(function, method="." in scope))
    ]
    found += lambda_tables(tree, names)
    return placed(path, source, names, found)
