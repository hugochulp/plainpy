"""slopcheck rules: each has a fixture that must be flagged and one that must not."""

from __future__ import annotations

from pathlib import Path

import pytest
from _common import Settings
from _findings import finding_lines, new_findings
from slopcheck.edit_rules import file_findings
from slopcheck.finish_rules import class_budget, project_findings

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
    assert [f for f in class_budget(project_findings({"m.py": source}), SETTINGS) if f.rule == "SLP006"] == []


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


def test_exception_budget_counts_only_new_exception_classes() -> None:
    errors = "".join(f"class E{n}(ValueError):\n    pass\n\n\n" for n in range(4))
    base = project_findings({"m.py": "class E0(ValueError):\n    pass\n"})
    within = project_findings({"m.py": errors.rsplit("class E3", 1)[0]})
    over = project_findings({"m.py": errors})
    assert [f for f in class_budget(new_findings(within, base), SETTINGS) if f.rule == "SLP011"] == []
    [budget] = [f for f in class_budget(new_findings(over, base), SETTINGS) if f.rule == "SLP011"]
    assert budget.message == "3 new exception classes, budget 2: E1 (m.py), E2 (m.py), E3 (m.py)"


@pytest.mark.parametrize(
    ("path", "expected"), [("pkg/utils.py", 1), ("pkg/helpers.py", 1), ("pkg/util.py", 0), ("tests/utils.py", 0)]
)
def test_grab_bag_module_names(path: str, expected: int) -> None:
    assert len([f for f in project_findings({path: "X = 1\n"}) if f.rule == "SLP010"]) == expected


def test_a_wide_module_warns_again_only_when_it_grows() -> None:
    def module(count: int) -> dict[str, str]:
        return {"m.py": "".join(f"def f{n}() -> int:\n    return {n}\n\n\n" for n in range(count))}

    base = project_findings(module(16))
    assert [f.rule for f in new_findings(project_findings(module(16)), base) if f.rule == "SLP010"] == []
    assert [f.rule for f in new_findings(project_findings(module(17)), base) if f.rule == "SLP010"] == ["SLP010"]
    assert [f for f in project_findings({"pkg/__init__.py": module(16)["m.py"]}) if f.rule == "SLP010"] == []


MODEL = "from pydantic import BaseModel\n\n\nclass User(BaseModel):{allow}\n    name: str\n"


def test_a_model_validated_only_in_tests_is_away_from_io() -> None:
    test = "from app import User\n\n\ndef test_user() -> None:\n    assert User.model_validate({'name': 'a'})\n"
    sources = {"app.py": MODEL.format(allow=""), "tests/test_app.py": test}
    assert [f.scope for f in project_findings(sources) if f.rule == "SLP003"] == ["User"]


def test_a_model_can_be_allowed() -> None:
    source = MODEL.format(allow="  # slopcheck: allow SLP003 -- shared with the web client")
    assert flagged("SLP003", source) == []


def test_calls_in_tests_count_as_passing_an_option() -> None:
    module = "def fetch(url: str, timeout: float = 30) -> str:\n    return f'{url} {timeout}'\n"
    test = "from app import fetch\n\n\ndef test_fetch() -> None:\n    assert fetch('a', timeout=1)\n"
    assert [f for f in project_findings({"app.py": module, "tests/test_app.py": test}) if f.rule == "SLP007"] == []


def test_overloads_count_once_towards_a_wide_module() -> None:
    overloads = (
        "@overload\ndef f(x: int) -> int: ...\n@overload\ndef f(x: str) -> str: ...\ndef f(x):\n    return x\n\n\n"
    )
    source = overloads + "".join(f"def g{n}() -> int:\n    return {n}\n\n\n" for n in range(14))
    assert flagged("SLP010", source) == []


@pytest.mark.parametrize(
    "source",
    [
        "from pydantic import BaseModel\nfrom pydantic_settings import BaseSettings\n\n\nclass Db(BaseModel):\n    url: str\n\n\n"
        "class Settings(BaseSettings):\n    db: Db\n",
        "from pydantic import BaseModel\nfrom sqlmodel import SQLModel\n\n\nclass Meta(BaseModel):\n    tags: list[str]\n\n\n"
        "class Hero(SQLModel, table=True):\n    meta: Meta\n",
        "from pydantic import BaseModel\n\n\nclass Item(BaseModel):\n    sku: str\n\n\nclass Order(BaseModel):\n"
        "    items: list['Item']\n\n\ndef load(d: dict) -> Order:\n    return Order.model_validate(d)\n",
        "from pydantic import BaseModel\n\n\nclass User(BaseModel):\n    name: str\n\n\n@app.post('/')\n"
        "def create(user: 'User') -> str:\n    return user.name\n",
        "from pydantic import BaseModel\n\n\nclass User(BaseModel):\n    name: str\n\n"
        "    def save(self, f) -> None:\n        f.write(self.model_dump_json())\n",
    ],
    ids=["settings field", "table field", "quoted field", "quoted handler type", "dumps itself"],
)
def test_models_that_reach_io_another_way(source: str) -> None:
    assert flagged("SLP003", source) == []


def test_a_no_op_implementing_a_project_protocol_is_not_a_placeholder() -> None:
    source = (
        "from typing import Protocol\n\n\nclass Sink(Protocol):\n    def write(self, s: str) -> None: ...\n\n\n"
        "class NullSink:\n    def write(self, s: str) -> None:\n        pass\n\n\nclass Real:\n    def write(self, s: str) -> None:\n"
        "        print(s)\n"
    )
    assert flagged("SLP008", source) == []


def test_grouped_model_findings_name_every_model() -> None:
    source = "from pydantic import BaseModel\n\n\nclass Tag(BaseModel):\n    name: str\n\n\nclass Note(BaseModel):\n    tags: list[Tag]\n"
    found = [f for f in project_findings({"m.py": source}) if f.rule == "SLP003"]
    assert finding_lines(found) == ["m.py SLP003 x2 at lines 4, 8: `Tag` never reaches I/O, `Note` never reaches I/O"]
