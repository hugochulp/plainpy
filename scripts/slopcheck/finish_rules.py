"""Project rules that gate runs when Claude finishes, over every Python file in the project.

`project_findings` reports SLP001, SLP003, SLP005, SLP007, SLP008 and SLP010, plus one hidden
SLP006 (class) or SLP011 (exception class) entry per class. Once the base comparison has kept
only the new ones, `class_budget` turns each budget that is exceeded into one finding.
"""

from __future__ import annotations

import ast
import builtins
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path

from _common import Settings
from _findings import Finding, is_test_file

from .classes import Function, ancestors, class_defs, hierarchy, name_of, qualnames
from .shared import base_names, functions, is_dunder, methods, parse, placed, statements

WARN_RULES = frozenset({"SLP003", "SLP005", "SLP007", "SLP010", "SLP011"})  # reported, never blocking
INTERFACE_BASES = {"ABC", "Protocol"}
MAX_HELPER_STATEMENTS = 3
MAX_PUBLIC_NAMES = 15
EXCEPTION_BUDGET = 2
GRAB_BAGS = {"utils.py", "helpers.py", "common.py"}
MODEL_BASES = {"BaseModel", "RootModel"}
NOT_MODELS = {"BaseSettings", "SQLModel"}  # settings and tables: the framework does their I/O
NOT_IO = {"isinstance", "issubclass", "cast"}  # take a class without it reaching I/O
AWAY_FROM_IO = "`{}` never reaches I/O"  # short, so a grouped report line names every model
BUILTIN_EXCEPTIONS = frozenset(
    name for name, value in vars(builtins).items() if isinstance(value, type) and issubclass(value, BaseException)
)


def is_interface(cls: ast.ClassDef) -> bool:
    metaclass = any(k.arg == "metaclass" and name_of(k.value) == "ABCMeta" for k in cls.keywords)
    return metaclass or bool(base_names(cls) & INTERFACE_BASES)


def is_exception(above: set[str]) -> bool:
    """`above` holds every ancestor's name."""
    return any(name in BUILTIN_EXCEPTIONS or name.endswith(("Error", "Exception", "Warning")) for name in above)


def one_implementation(interface: ast.ClassDef, key: str, concrete: list[tuple[str, set[str], set[str]]]) -> str | None:
    """SLP001: an ABC or Protocol that only one class implements, by subclassing it (at any
    depth) or, for a Protocol, by defining all its methods. `key` is the interface's full id."""
    wanted = {m.name for m in methods(interface)} - set(dir(object))  # every class has these
    protocol = "Protocol" in base_names(interface)
    if protocol and "__call__" in wanted:
        return None  # plain functions satisfy a callback Protocol
    found = [name for name, above, own in concrete if key in above or (protocol and wanted and wanted <= own)]
    if len(found) > 1 or (protocol and not found):  # a Protocol may describe types from outside the project
        return None
    if not found:
        return f"`{interface.name}` has no implementation: drop it"
    return f"`{interface.name}` has one implementation, `{found[0]}`: use `{found[0]}` directly and drop the interface"


def names_in(node: ast.AST) -> set[str]:
    """Names and attributes in a node, those in quoted annotations (`"User"`, `list["Item"]`) included."""
    found = {name_of(n) for n in ast.walk(node) if isinstance(n, ast.Name | ast.Attribute)}
    quoted = (parse(n.value) for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str))
    return found.union(*(names_in(tree) for tree in quoted if tree))


def dumps(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr.startswith("model_dump") for n in ast.walk(node))


def passed(call: ast.Call) -> set[str]:
    """Names a call takes to I/O: classes passed to it (as SDKs take a schema), a class it
    validates or asks for a schema, and the class it builds from an unpacked dict."""
    callee = name_of(call.func)
    if callee in NOT_IO:
        return set()
    values = [*call.args, *(k.value for k in call.keywords)]
    found = {name for v in values if isinstance(v, ast.Name | ast.Attribute | ast.Subscript) for name in names_in(v)}
    if isinstance(call.func, ast.Attribute) and call.func.attr.startswith(
        ("model_validate", "model_json_schema", "parse_")
    ):
        found |= names_in(call.func)
    if any(k.arg is None for k in call.keywords):
        found.add(callee)
    return found


def handled(function: Function, *, may_dump: bool) -> set[str]:
    """Every name in a function that dumps a model, and the types in a decorated handler's signature.
    `may_dump` says whether the file mentions `model_dump` at all, so other files skip the walk."""
    if may_dump and dumps(function):
        return names_in(function)
    if not function.decorator_list:
        return set()
    annotations = [p.annotation for p in ast.walk(function.args) if isinstance(p, ast.arg)] + [function.returns]
    return {name for node in annotations if node for name in names_in(node)}


def reaching_io(
    files: Iterable[tuple[ast.Module, str]], models: set[ast.ClassDef], framework: set[str], renamed: dict[str, str]
) -> set[str]:
    """SLP003: names of models that reach I/O, directly or as a field type, base or subclass of
    one that does. `files` holds each non-test file's tree and source; `framework` names the
    settings and table classes, whose I/O the framework does; `models` includes them;
    `renamed` maps import aliases to the names they stand for."""
    found = framework | {cls.name for cls in models if dumps(cls)}  # a model that dumps itself
    for tree, source in files:
        may_dump = "model_dump" in source
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                found |= passed(node)
            elif isinstance(node, Function):
                found |= handled(node, may_dump=may_dump)
    found |= {renamed[name] for name in found & renamed.keys()}
    links: defaultdict[str, set[str]] = defaultdict(set)
    known = {cls.name for cls in models}  # never `BaseModel`, which would link every model to every other
    for cls in models:
        fields = (node.annotation for node in cls.body if isinstance(node, ast.AnnAssign))
        links[cls.name] |= {name for field in fields for name in names_in(field)}
        for base in base_names(cls) & known:
            links[cls.name].add(base)
            links[base].add(cls.name)
    return found.union(*(ancestors(name, links) for name in found & links.keys()))


def references(trees: Iterable[ast.Module]) -> tuple[Counter[str], defaultdict[str, list[ast.Call]], dict[str, str]]:
    """How often each name is read anywhere (as a name or as an attribute), the calls to each
    name, and what each import alias stands for (`request` for `from x import fetch as request`).
    Reads and calls through an alias also count for the name it stands for."""
    names: Counter[str] = Counter()
    calls: defaultdict[str, list[ast.Call]] = defaultdict(list)
    renamed: dict[str, str] = {}
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Name | ast.Attribute) and isinstance(node.ctx, ast.Load):
                names[name_of(node)] += 1
            elif isinstance(node, ast.Call):
                calls[name_of(node.func)].append(node)
            elif isinstance(node, ast.ImportFrom):
                renamed |= {a.asname: a.name for a in node.names if a.asname and a.asname != a.name}
    for alias, name in renamed.items():
        names[name] += names[alias]
        calls[name] += calls[alias]
    return names, calls, renamed


def single_use_helper(function: Function, uses: Counter[str]) -> str | None:
    """SLP005: a tiny private helper that is used once."""
    name, size = function.name, len(statements(function))
    private = name.startswith("_") and not is_dunder(name)
    if not private or function.decorator_list or size >= MAX_HELPER_STATEMENTS or uses[name] != 1:
        return None
    return f"`{name}` is used once and has {size} statement(s): inline it where it is used"


def unused_options(function: Function, calls: list[ast.Call], *, method: bool) -> list[str]:
    """SLP007: parameters with a default that no call passes, by name or by position."""
    by_name = {k.arg for c in calls for k in c.keywords}  # None: a `**` call
    if not calls or None in by_name or any(isinstance(a, ast.Starred) for c in calls for a in c.args):
        return []
    a = function.args
    positional = [p.arg for p in [*a.posonlyargs, *a.args]]
    most = max(len(c.args) for c in calls) + method  # a method call's receiver is its first argument
    options = positional[max(most, len(positional) - len(a.defaults)) :]
    options += [k.arg for k, default in zip(a.kwonlyargs, a.kw_defaults, strict=True) if default is not None]
    return [p for p in options if p not in by_name]


def placeholder(function: Function, *, method: bool) -> str | None:
    """SLP008: a body that does nothing yet, or returns a constant while ignoring every parameter."""
    body = statements(function)
    stmt = body[0] if len(body) == 1 else None
    params = [p for p in ast.walk(function.args) if isinstance(p, ast.arg)][method:]  # in order: self first
    exc = stmt.exc if isinstance(stmt, ast.Raise) else None
    kinds = {
        "only a docstring": not body,
        "only `pass`": isinstance(stmt, ast.Pass),
        "only `...`": isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is ...,
        "only `raise NotImplementedError`": exc is not None
        and name_of(exc.func if isinstance(exc, ast.Call) else exc) == "NotImplementedError",
        "a constant, ignoring its parameters": isinstance(stmt, ast.Return)
        and (stmt.value is None or isinstance(stmt.value, ast.Constant))
        and bool(params),
    }
    kind = next((kind for kind, hit in kinds.items() if hit), None)
    return f"`{function.name}` is a placeholder ({kind}): write the real code or delete it" if kind else None


def function_findings(
    function: Function, scope: str, uses: Counter[str], calls: list[ast.Call], *, exempt: bool
) -> list[Finding]:
    """SLP005, SLP007 and SLP008 for one function. `calls` are the calls to its name, anywhere.
    `exempt` for decorated and special methods, and methods a base class or a subclass may share."""
    method = "." in scope
    messages = {
        "SLP005": single_use_helper(function, uses),
        "SLP008": None if exempt else placeholder(function, method=method),
    }
    found = [Finding(rule, function.lineno, message, scope, "") for rule, message in messages.items() if message]
    if exempt or function.name.startswith("_") or uses[function.name] > len(calls):  # or passed around as a value
        return found
    return found + [
        Finding("SLP007", function.lineno, f"no call passes `{p}` to `{function.name}`: drop the option", scope, p)
        for p in unused_options(function, calls, method=method)
    ]


def nested_placeholders(
    names: dict[ast.AST, str], seen: set[Function], uses: Counter[str], calls: defaultdict[str, list[ast.Call]]
) -> list[Finding]:
    """SLP008 for functions inside functions (`names` is the file's `qualnames`; `seen` are the
    module-level functions and methods). Skips decorated and special ones, and callbacks passed
    around as values, such as a signal handler that deliberately does nothing."""
    nested = [n for n in names if isinstance(n, Function) and n not in seen and not n.decorator_list]
    kept = [n for n in nested if not is_dunder(n.name) and uses[n.name] <= len(calls[n.name])]
    return [Finding("SLP008", n.lineno, m, names[n], "") for n in kept if (m := placeholder(n, method=False))]


def over_wide(path: str, tree: ast.Module) -> list[Finding]:
    """SLP010: a module with too many public functions and classes, or a grab-bag name. Whole-file
    findings, so no allow comment covers them."""
    name = Path(path).name
    public = {n.name for n in tree.body if isinstance(n, Function | ast.ClassDef) and not n.name.startswith("_")}
    grab_bag = f"`{name}` is a grab-bag name: move each function next to its callers"
    found = [Finding("SLP010", 0, grab_bag, "<module>", name, file=path)] if name in GRAB_BAGS else []
    if len(public) > MAX_PUBLIC_NAMES and name != "__init__.py":
        message = f"{len(public)} public functions and classes (limit {MAX_PUBLIC_NAMES}): split it by what callers use together"
        found.append(Finding("SLP010", 0, message, "<module>", "", value=len(public), file=path))
    return found


def project_findings(sources: dict[str, str]) -> list[Finding]:
    """Project rules over {path: source}; see the module docstring."""
    trees = {path: tree for path, source in sources.items() if not path.endswith(".pyi") and (tree := parse(source))}
    names = {path: qualnames(tree) for path, tree in trees.items() if not is_test_file(Path(path))}
    classes = {path: class_defs(found) for path, found in names.items()}
    ids, above = hierarchy(names)
    own: dict[str, set[str]] = {}
    for cls, key in ids.items():
        own.setdefault(key, set()).update(m.name for m in methods(cls))
    concrete = [  # (qualname, ancestors, method names, inherited ones included) for each non-interface
        (names[path][cls], above[ids[cls]], set().union(*(own.get(key, set()) for key in {ids[cls], *above[ids[cls]]})))
        for path, here in classes.items()
        for cls in here
        if not is_interface(cls)
    ]
    pydantic = {cls for cls in ids if above[ids[cls]] & (MODEL_BASES | NOT_MODELS)}
    models = {cls for cls in pydantic if not above[ids[cls]] & NOT_MODELS}
    framework = {cls.name for cls in pydantic - models}
    uses, calls, renamed = references(trees.values())  # tests count as uses and calls
    files = [(trees[path], sources[path]) for path in names]
    at_io = reaching_io(files, pydantic, framework, renamed) if models else set[str]()
    subclassed = set[str]().union(*above.values())
    overridable = {m for cls in ids if base_names(cls) or {cls.name, ids[cls]} & subclassed for m in methods(cls)}
    promised = {m.name for cls in ids if is_interface(cls) for m in methods(cls)}  # a Protocol's no-op may be real
    found: list[Finding] = []
    for path, here in classes.items():
        defs, qual = functions(trees[path], here, names[path]), names[path]
        candidates = [
            Finding("SLP001", cls.lineno, message, qual[cls], "")
            for cls in here
            if is_interface(cls) and (message := one_implementation(cls, ids[cls], concrete))
        ]
        away = [cls for cls in here if cls in models and cls.name not in at_io]
        candidates += [Finding("SLP003", cls.lineno, AWAY_FROM_IO.format(cls.name), qual[cls], "") for cls in away]
        candidates += [  # counted by `class_budget`
            Finding("SLP011" if is_exception(above[ids[cls]]) else "SLP006", cls.lineno, qual[cls], qual[cls], "")
            for cls in here
        ]
        for function, scope in defs:
            shared = function in overridable or ("." in scope and function.name in promised)
            exempt = bool(function.decorator_list) or is_dunder(function.name) or shared
            candidates += function_findings(function, scope, uses, calls[function.name], exempt=exempt)
        candidates += nested_placeholders(qual, {function for function, _ in defs}, uses, calls)
        found += placed(path, sources[path], [n for n in qual if isinstance(n, ast.ClassDef | Function)], candidates)
        found += over_wide(path, trees[path])
    return found


def class_budget(new: list[Finding], settings: Settings) -> list[Finding]:
    """SLP006 and SLP011: the new classes and the new exception classes, each as one finding if
    there are more than its budget, else none."""
    budgets = {"SLP006": ("classes", settings.class_budget), "SLP011": ("exception classes", EXCEPTION_BUDGET)}
    kept = [f for f in new if f.rule not in budgets]
    for rule, (what, budget) in budgets.items():
        added = [f for f in new if f.rule == rule]
        if len(added) > budget:
            names = ", ".join(f"{f.message} ({f.file})" for f in added)
            key = ",".join(sorted(f.message for f in added))
            kept.append(Finding(rule, 0, f"{len(added)} new {what}, budget {budget}: {names}", "", key))
    return kept
