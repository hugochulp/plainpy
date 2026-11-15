"""slopcheck: plainpy's own checks for over-abstraction, using only the stdlib `ast`.

Single-file rules run on every edit (via check_file); project rules and the class budget
run in the finish check (via gate), over every Python file in the project. Test files and
stubs are never judged. A rule can be allowed on one definition with a comment on its
`def` or `class` header (any line of it, since the formatter may wrap a long header):
`# slopcheck: allow SLP004 -- reason of three or more words`.
"""

from __future__ import annotations

import ast
import re
import warnings
from collections import Counter
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from _common import Settings
from _findings import Finding, is_test_file

WARN_RULES = frozenset({"SLP005"})  # reported to the user, never blocking
ALLOW_CODES = r"SLP\d+(?:\s*,\s*SLP\d+)*"  # also used by guard_edit, so both read the same syntax
ALLOW = re.compile(rf"#\s*slopcheck:\s*allow\s+(?P<codes>{ALLOW_CODES})", re.IGNORECASE)
INTERFACE_BASES = {"ABC", "Protocol"}
MAX_HELPER_STATEMENTS = 3

Function = ast.FunctionDef | ast.AsyncFunctionDef


def parse(source: str) -> ast.Module | None:
    try:
        with warnings.catch_warnings(action="ignore"):  # SyntaxWarning would print to the hook's output
            return ast.parse(source)
    except (SyntaxError, ValueError):  # ValueError: null bytes, on Python 3.11
        return None


def placed(path: str, source: str, definitions: list[ast.ClassDef | Function], found: list[Finding]) -> list[Finding]:
    """Fill in each finding's file, and drop the ones an allow comment on the header of its
    definition covers. Findings carry no line text: their scope names the definition, so
    editing its header doesn't make an old finding look new."""
    if not found:
        return []
    lines = source.splitlines()
    body_starts = {node.lineno: node.body[0].lineno for node in definitions}
    kept = []
    for finding in found:
        header = lines[finding.line - 1 : max(finding.line, body_starts[finding.line] - 1)]
        allowed = [
            code
            for line in header
            if (match := ALLOW.search(line))
            for code in re.findall(r"SLP\d+", match["codes"].upper())
        ]
        if finding.rule not in allowed:
            kept.append(replace(finding, file=path))
    return kept


def name_of(node: ast.expr) -> str:
    """`Protocol` for `Protocol`, `typing.Protocol` and `Protocol[T]`."""
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def base_names(cls: ast.ClassDef) -> set[str]:
    return {name_of(base) for base in cls.bases} - {"object", ""}


def is_interface(cls: ast.ClassDef) -> bool:
    metaclass = any(k.arg == "metaclass" and name_of(k.value) == "ABCMeta" for k in cls.keywords)
    return metaclass or bool(base_names(cls) & INTERFACE_BASES)


def is_exception(cls: ast.ClassDef) -> bool:
    return any(name.endswith(("Error", "Exception", "Warning")) for name in base_names(cls))


def statements(function: Function) -> list[ast.stmt]:
    """The body without its docstring."""
    return function.body[1:] if ast.get_docstring(function, clean=False) is not None else function.body


def is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def methods(cls: ast.ClassDef) -> list[Function]:
    return [node for node in cls.body if isinstance(node, Function)]


def class_defs(tree: ast.Module) -> list[ast.ClassDef]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]


def functions(tree: ast.Module, classes: list[ast.ClassDef]) -> list[tuple[Function, str]]:
    """Module-level functions and methods, with their scope name (`f` or `Class.f`)."""
    found = [(node, node.name) for node in tree.body if isinstance(node, Function)]
    return found + [(method, f"{cls.name}.{method.name}") for cls in classes for method in methods(cls)]


# --- single-file rules (every edit) -----------------------------------------------------


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
    """(name, passed by position) for each argument, if every one is a bare parameter name."""
    names = []
    for arg in call.args:
        value = arg.value if isinstance(arg, ast.Starred) else arg
        if not isinstance(value, ast.Name):
            return None
        names.append((value.id, True))
    for keyword in call.keywords:
        if not isinstance(keyword.value, ast.Name) or keyword.arg not in {None, keyword.value.id}:
            return None
        names.append((keyword.value.id, False))
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
    if method and positional[:1] == ["self"]:
        positional = positional[1:]
    params = [*positional, *(a.arg for a in arguments.kwonlyargs)]
    params += [a.arg for a in (arguments.vararg, arguments.kwarg) if a]
    names = forwarded(value)
    if not params or names is None or sorted(name for name, _ in names) != sorted(params):
        return None
    in_order = [name for name, by_position in names if by_position and name in positional]
    if in_order != sorted(in_order, key=positional.index):
        return None  # reordered arguments: an adapter, not a pass-through
    callee = ast.unparse(value.func)
    return f"`{function.name}` only passes its arguments on to `{callee}`: call `{callee}` directly"


def file_findings(source: str, path: str) -> list[Finding]:
    """Single-file rules for one file: SLP002 and SLP004."""
    tree = None if is_test_file(Path(path)) or path.endswith(".pyi") else parse(source)
    if tree is None:
        return []
    classes = class_defs(tree)
    standalone = [cls for cls in classes if not base_names(cls)]  # a subclass's methods may be called by its base
    defs = functions(tree, standalone)
    found = [
        Finding("SLP002", cls.lineno, message, cls.name, "") for cls in classes if (message := class_as_function(cls))
    ]
    found += [
        Finding("SLP004", function.lineno, message, scope, "")
        for function, scope in defs
        if (message := pass_through(function, method="." in scope))
    ]
    return placed(path, source, [*classes, *(function for function, _ in defs)], found)


# --- project rules (finish) ---------------------------------------------------------------

Concrete = tuple[str, set[str], set[str]]  # a class that isn't an interface: (name, ancestors, method names)


def ancestors(name: str, parents: dict[str, set[str]]) -> set[str]:
    """Every base above a class, by name, through any number of levels."""
    seen: set[str] = set()
    todo = [name]
    while todo:
        for base in parents.get(todo.pop(), set()) - seen:
            seen.add(base)
            todo.append(base)
    return seen


def one_implementation(interface: ast.ClassDef, concrete: list[Concrete]) -> str | None:
    """SLP001: an ABC or Protocol that only one class implements, by subclassing it (at any
    depth) or, for a Protocol, by defining all its methods."""
    wanted = {m.name for m in methods(interface) if not is_dunder(m.name)}
    protocol = "Protocol" in base_names(interface)
    found = [
        name for name, above, own in concrete if interface.name in above or (protocol and wanted and wanted <= own)
    ]
    if len(found) > 1 or (protocol and not found):  # a Protocol may describe types from outside the project
        return None
    if not found:
        return f"`{interface.name}` has no implementation: drop it"
    return f"`{interface.name}` has one implementation, `{found[0]}`: use `{found[0]}` directly and drop the interface"


def references(trees: Iterable[ast.Module]) -> Counter[str]:
    """How often each name is read anywhere: as a name or as an attribute."""
    names: Counter[str] = Counter()
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Name | ast.Attribute) and isinstance(node.ctx, ast.Load):
                names[node.id if isinstance(node, ast.Name) else node.attr] += 1
    return names


def single_use_helper(function: Function, uses: Counter[str]) -> str | None:
    """SLP005: a tiny private helper that is used once."""
    name, size = function.name, len(statements(function))
    private = name.startswith("_") and not is_dunder(name)
    if not private or function.decorator_list or size >= MAX_HELPER_STATEMENTS or uses[name] != 1:
        return None
    return f"`{name}` is used once and has {size} statement(s): inline it where it is used"


def project_findings(sources: dict[str, str]) -> list[Finding]:
    """Project rules over {path: source}: SLP001, SLP005, and one SLP006 entry per class for
    `class_budget` to count once the base comparison has kept only the new ones."""
    trees = {path: tree for path, source in sources.items() if not path.endswith(".pyi") and (tree := parse(source))}
    classes = {path: class_defs(tree) for path, tree in trees.items() if not is_test_file(Path(path))}
    every = [cls for found in classes.values() for cls in found]
    parents: dict[str, set[str]] = {}
    for cls in every:
        parents.setdefault(cls.name, set()).update(base_names(cls))
    concrete = [
        (cls.name, ancestors(cls.name, parents), {m.name for m in methods(cls)})
        for cls in every
        if not is_interface(cls)
    ]
    uses = references(trees.values())  # tests count as uses
    found = []
    for path, here in classes.items():
        defs = functions(trees[path], here)
        candidates = [
            Finding("SLP001", cls.lineno, message, cls.name, "")
            for cls in here
            if is_interface(cls) and (message := one_implementation(cls, concrete))
        ]
        candidates += [
            Finding("SLP005", function.lineno, message, scope, "")
            for function, scope in defs
            if (message := single_use_helper(function, uses))
        ]
        candidates += [Finding("SLP006", cls.lineno, cls.name, cls.name, "") for cls in here if not is_exception(cls)]
        found += placed(path, sources[path], [*here, *(function for function, _ in defs)], candidates)
    return found


def class_budget(new: list[Finding], settings: Settings) -> list[Finding]:
    """SLP006: the new classes, as one finding if there are more than the budget, else none."""
    added = [f for f in new if f.rule == "SLP006"]
    rest = [f for f in new if f.rule != "SLP006"]
    if len(added) <= settings.class_budget:
        return rest
    names = ", ".join(f"{f.message} ({f.file})" for f in added)
    message = f"{len(added)} new classes, budget {settings.class_budget}: {names}"
    return [*rest, Finding("SLP006", 0, message, "", ",".join(sorted(f.message for f in added)))]
