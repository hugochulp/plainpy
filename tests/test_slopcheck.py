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


def test_an_upper_case_allow_counts() -> None:
    source = "def legacy(a):  # SLOPCHECK: allow SLP004 -- old name kept for callers\n    return area(a)\n"
    assert flagged("SLP004", source) == []


def test_protocol_special_methods_must_be_implemented() -> None:
    source = (
        "from typing import Protocol\n\n\nclass Source(Protocol):\n    def read(self) -> str: ...\n"
        "    def __enter__(self) -> 'Source': ...\n\n\nclass Plain:\n    def read(self) -> str:\n        return ''\n"
    )
    assert flagged("SLP001", source) == []


def test_nested_classes_keep_their_full_names() -> None:
    method = "        def run(self, x):\n            return go(x)\n"
    source = f"class A:\n    class Inner:\n{method}\n\nclass B:\n    class Inner:\n{method}"
    assert flagged("SLP004", source) == ["A.Inner.run", "B.Inner.run"]


STORE = "from abc import ABC\n\n\nclass Store(ABC):\n    pass\n\n\nclass Impl(Store):\n    pass\n"


@pytest.mark.parametrize(
    ("path", "second", "expected"),
    [
        ("pkg/c.py", "from pkg.a import Store as BaseStore\n\n\nclass Other(BaseStore):\n    pass\n", []),
        ("pkg/c.py", "from pkg.b import Store as BaseStore\n\n\nclass Other(BaseStore):\n    pass\n", ["Store"]),
        ("pkg/c.py", "import pkg.a as a\n\n\nclass Other(a.Store):\n    pass\n", []),
        ("pkg/c.py", "from . import b\n\n\nclass Other(b.Store):\n    pass\n", ["Store"]),
        ("pkg/sub/c.py", "from ..a import Store\n\n\nclass Other(Store):\n    pass\n", []),
        ("pkg/c.py", "from pkg import Store\n\n\nclass Other(Store):\n    pass\n", []),  # re-exported by pkg
        ("pkg/c.py", "from pkg.b import *\n\n\nclass Other(Store):\n    pass\n", []),  # unresolved: by short name
    ],
)
def test_bases_are_found_through_imports(path: str, second: str, expected: list[str]) -> None:
    sources = {
        "src/pkg/__init__.py": "from .a import Store\n",
        "src/pkg/a.py": STORE,
        "src/pkg/b.py": "class Store:\n    pass\n",
        f"src/{path}": second,
    }
    assert [f.scope for f in project_findings(sources) if f.rule == "SLP001"] == expected


def test_classes_that_share_a_short_name_stay_apart() -> None:
    other = "class Store:\n    pass\n\n\nclass Impl(ValueError):\n    pass\n\n\nclass Other(Store):\n    pass\n"
    found = project_findings({"pkg/a.py": STORE, "pkg/b.py": other})
    assert [(f.file, f.scope) for f in found if f.rule == "SLP001"] == [("pkg/a.py", "Store")]
    assert ("pkg/a.py", "Impl") in [(f.file, f.scope) for f in found if f.rule == "SLP006"]


def test_a_name_re_exported_as_its_own_module_does_not_loop() -> None:
    sources = {
        "pkg/__init__.py": "from .main import main\n",  # like unittest: `pkg.main` is a module and a function
        "pkg/main.py": STORE,
        "pkg/c.py": "import pkg.main\n\n\nclass Other(pkg.main.Missing):\n    pass\n",
    }
    assert [f.scope for f in project_findings(sources) if f.rule == "SLP001"] == ["Store"]


BASE = "from abc import ABC\n\n\nclass Base(ABC):\n    pass\n\n\nclass One(Base):\n    pass\n"


@pytest.mark.parametrize(
    "second",
    [
        {"pkg/c.py": "from pkg.a import Base\n\n\nclass Base(Base):\n    pass\n"},  # named like its base
        {
            "pkg/c.py": "from pkg.a import Base\n\n\nclass Two(Base):\n    pass\n\n\ndef f():\n    from pkg.b import Base\n"
        },
        {
            "pkg/c.py": "try:\n    from pkg.a import Base\nexcept ImportError:\n    from pkg.b import Base\n\n\nclass Two(Base):\n    pass\n"
        },
        {
            "pkg/a.py": BASE
            + "\n\nclass A:\n    class Base:\n        pass\n\n    class C:\n        class Two(Base):\n            pass\n"
        },
        {
            "pkg/a.py": BASE
            + "\n\nclass A:\n    class Base:\n        pass\n\n    def make(self):\n        class Two(Base):\n            pass\n"
        },
        {
            "config.py": BASE,
            "scripts/config.py": BASE,
            "scripts/c.py": "from config import Base\n\n\nclass Two(Base):\n    pass\n",
        },
    ],
)
def test_a_base_is_never_resolved_to_the_wrong_class(second: dict[str, str]) -> None:
    sources = {"pkg/a.py": BASE, "pkg/b.py": "class Base:\n    pass\n"} | second
    assert [(f.file, f.scope) for f in project_findings(sources) if f.rule == "SLP001"] == []


def test_a_subclass_named_like_its_exception_base_is_an_exception() -> None:
    sources = {
        "errors.py": "class Problem(Exception):\n    pass\n",
        "x.py": "from errors import Problem\n\n\nclass Problem(Problem):\n    pass\n",
    }
    assert [f.scope for f in project_findings(sources) if f.rule == "SLP006"] == []


def test_protocols_do_not_require_what_object_provides() -> None:
    source = (
        "from typing import Protocol\n\n\nclass Key(Protocol):\n    def name(self) -> str: ...\n    def __hash__(self) -> int: ...\n\n\n"
        "class A:\n    def name(self) -> str:\n        return 'a'\n\n    def __hash__(self) -> int:\n        return 1\n\n\n"
        "class B:\n    def name(self) -> str:\n        return 'b'\n"
    )
    assert flagged("SLP001", source) == []


def test_a_long_dotted_base_does_not_crash() -> None:
    assert flagged("SLP001", f"class X({'.'.join(f'a{n}' for n in range(400))}):\n    pass\n") == []
