"""slopcheck rules: each has a fixture that must be flagged and one that must not."""

from __future__ import annotations

from pathlib import Path

import pytest
from _common import Settings
from _findings import new_findings
from slopcheck import class_budget, file_findings, project_findings

FIXTURES = Path(__file__).resolve().parent / "slopcheck"
SETTINGS = Settings(profile="standard", blocking=True, cognitive_max=15, mccabe_max=10)


def flagged(rule: str, source: str, path: str = "module.py") -> list[str]:
    """Names (enclosing scopes) the rule flags in a one-file project."""
    found = file_findings(source, path) + project_findings({path: source})
    return sorted(f.scope for f in found if f.rule == rule)


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("slp*.py")), ids=lambda p: p.stem)
def test_fixture(fixture: Path) -> None:
    source = fixture.read_text()
    expected = [name.strip() for name in source.splitlines()[0].removeprefix("# expect:").split(",") if name.strip()]
    rule = fixture.stem.split("_")[0].upper()
    assert flagged(rule, source) == expected


@pytest.mark.parametrize("path", ["tests/test_doubler.py", "typings/doubler.pyi"])
def test_test_files_and_stubs_are_never_judged(path: str) -> None:
    assert flagged("SLP002", (FIXTURES / "slp002_fail.py").read_text(), path=path) == []


def test_test_fakes_are_not_a_second_implementation() -> None:
    interface = "from abc import ABC\n\n\nclass Store(ABC):\n    pass\n\n\nclass FileStore(Store):\n    pass\n"
    fake = "from app import Store\n\n\nclass FakeStore(Store):\n    pass\n"
    found = project_findings({"app.py": interface, "tests/test_app.py": fake})
    assert [f.rule for f in found if f.rule == "SLP001"] == ["SLP001"]


def test_a_use_in_tests_counts_for_private_helpers() -> None:
    module = (FIXTURES / "slp005_fail.py").read_text()
    test = "from module import _double\n\n\ndef test_double() -> None:\n    assert _double(2) == 4\n"
    found = project_findings({"module.py": module, "tests/test_module.py": test})
    assert not [f for f in found if f.rule == "SLP005"]


def classes(names: list[str]) -> str:
    return "".join(f"class {name}:\n    x = 1\n\n\n" for name in names)


def test_class_budget_counts_only_new_classes() -> None:
    base = project_findings({"m.py": classes(["Old"])})
    within = project_findings({"m.py": classes(["Old", "A", "B", "C"])})
    over = project_findings({"m.py": classes(["Old", "A", "B", "C", "D"])})
    assert class_budget(new_findings(within, base), SETTINGS) == []
    [budget] = class_budget(new_findings(over, base), SETTINGS)
    assert budget.rule == "SLP006"
    assert budget.message.startswith("4 new classes, budget 3: A (m.py), B (m.py)")


def test_class_budget_skips_exceptions_and_allowed_classes() -> None:
    source = (
        classes(["A", "B", "C"])
        + "class DomainFailure(Exception):\n    pass\n\n\nclass Retryable(DomainFailure):\n    pass\n\n\n"
        + "class Stop(KeyboardInterrupt):\n    pass\n\n\n"
        + "class Kept:  # slopcheck: allow SLP006 -- mirrors the wire format\n    x = 1\n"
    )
    assert class_budget(project_findings({"m.py": source}), SETTINGS) == []


@pytest.mark.parametrize("middle", ["Polygon(Shape)", "Polygon(Shape, ABC)"])
def test_implementations_count_at_any_depth(middle: str) -> None:
    source = (
        f"from abc import ABC\n\n\nclass Shape(ABC):\n    pass\n\n\nclass {middle}:\n    pass\n\n\n"
        "class Square(Polygon):\n    pass\n\n\nclass Triangle(Polygon):\n    pass\n"
    )
    assert flagged("SLP001", source) == []


def test_methods_a_base_class_may_call_are_not_pass_throughs() -> None:
    source = "import logging\n\n\nclass Forward(logging.Handler):\n    def emit(self, record):\n        self.target.emit(record)\n"
    assert flagged("SLP004", source) == []


def test_editing_a_class_header_does_not_make_it_new() -> None:
    base = project_findings({"m.py": classes(["A", "B"])})
    edited = project_findings({"m.py": "class A(Generic[T]):\n    x = 1\n\n\nclass B:  # noqa: E501\n    x = 1\n"})
    assert class_budget(new_findings(edited, base), SETTINGS) == []


def test_editing_a_function_header_does_not_make_its_finding_new() -> None:
    base = file_findings("def size(a):\n    return len(a)\n", "m.py")
    edited = file_findings("def size(items: list[int]) -> int:\n    return len(items)\n", "m.py")
    assert base
    assert new_findings(edited, base) == []


def test_null_bytes_do_not_crash() -> None:
    assert file_findings("x = 1\0\n", "module.py") == []


def test_an_allow_inside_a_string_does_not_count() -> None:
    source = 'def wrap(x, tag="# slopcheck: allow SLP004 -- not a comment at all"):\n    return go(x, tag)\n'
    assert flagged("SLP004", source) == ["wrap"]


def test_packing_or_unpacking_arguments_is_not_a_pass_through() -> None:
    source = (
        "def batch(*items):\n    return target(items)\n\n\ndef spread(x):\n    return target(*x)\n\n\n"
        "def options(**kw):\n    return target(kw)\n\n\ndef forward(*args, **kw):\n    return target(*args, **kw)\n"
    )
    assert flagged("SLP004", source) == ["forward"]


def test_inherited_methods_satisfy_a_protocol() -> None:
    source = (
        "from typing import Protocol\n\n\nclass Pinger(Protocol):\n    def ping(self) -> None: ...\n\n\n"
        "class A:\n    def ping(self) -> None:\n        print(1)\n\n\nclass B(A):\n    pass\n"
    )
    assert flagged("SLP001", source) == []
