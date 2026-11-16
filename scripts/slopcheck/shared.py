"""Parsing, allow comments and small AST helpers shared by the edit and finish rules (and guard_edit)."""

from __future__ import annotations

import ast
import io
import re
import tokenize
import warnings
from dataclasses import replace

from _findings import Finding

from .classes import Function, name_of

ALLOW_CODES = r"SLP\d+(?:\s*,\s*SLP\d+)*"  # also used by guard_edit, so both read the same syntax
ALLOW = re.compile(rf"#\s*slopcheck:\s*allow\s+(?P<codes>{ALLOW_CODES})", re.IGNORECASE)


def parse(source: str) -> ast.Module | None:
    try:
        with warnings.catch_warnings(action="ignore"):  # SyntaxWarning would print to the hook's output
            return ast.parse(source)
    except (SyntaxError, ValueError):  # ValueError: null bytes, on Python 3.11
        return None


def comments_by_line(source: str) -> dict[int, str]:
    """Real comments only, so a suppression inside a string literal doesn't count."""
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        return {token.start[0]: token.string for token in tokens if token.type == tokenize.COMMENT}
    except (tokenize.TokenError, SyntaxError):
        return {n: line[line.index("#") :] for n, line in enumerate(source.splitlines(), 1) if "#" in line}


def placed(path: str, source: str, names: dict[ast.AST, str], found: list[Finding]) -> list[Finding]:
    """Fill in each finding's file, and drop the ones an allow comment on the header of its
    definition (a class or function among the file's `qualnames`) covers. Findings carry no line
    text: their scope names the definition, so editing its header doesn't make an old finding look new."""
    if not found:
        return []
    body_starts = {node.lineno: node.body[0].lineno for node in names if isinstance(node, ast.ClassDef | Function)}
    comments = comments_by_line(source) if "slopcheck" in source.lower() else {}
    kept = []
    for finding in found:
        header = range(finding.line, max(finding.line, body_starts[finding.line] - 1) + 1)
        allowed = [
            code
            for line in header
            if (match := ALLOW.search(comments.get(line, "")))
            for code in re.findall(r"SLP\d+", match["codes"].upper())
        ]
        if finding.rule not in allowed:
            kept.append(replace(finding, file=path))
    return kept


def base_names(cls: ast.ClassDef) -> set[str]:
    return {name_of(base) for base in cls.bases} - {"object", ""}


def statements(function: Function) -> list[ast.stmt]:
    """The body without its docstring."""
    return function.body[1:] if ast.get_docstring(function, clean=False) is not None else function.body


def is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def methods(cls: ast.ClassDef) -> list[Function]:
    return [node for node in cls.body if isinstance(node, Function)]


def functions(tree: ast.Module, classes: list[ast.ClassDef], names: dict[ast.AST, str]) -> list[tuple[Function, str]]:
    """Module-level functions and methods, with their scope: the qualname (`f`, `Outer.Inner.f`)."""
    found = [node for node in tree.body if isinstance(node, Function)]
    return [(node, names[node]) for node in found + [method for cls in classes for method in methods(cls)]]
