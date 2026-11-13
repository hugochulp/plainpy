"""Report layout: repeated rules grouped, each fix hint printed once."""

from __future__ import annotations

from pathlib import Path

from _common import Settings
from check_file import Finding, Run, format_report

PROJECT = Path("/project")
RUN = Run(
    path=PROJECT / "shipping.py",
    project=PROJECT,
    settings=Settings(profile="standard", blocking=True, cognitive_max=15, mccabe_max=10),
    ruff=["ruff"],
    complexipy=["complexipy"],
)


def finding(rule: str, line: int, message: str, value: int | None = None) -> Finding:
    return Finding(rule, line, message, scope="shipping_cost", text="", value=value)


# The report from the first live run: 13 findings across 6 rules.
LIVE_RUN = [
    finding("C901", 1, "`shipping_cost` is too complex (26 > 10)", 26),
    finding("PLR0912", 1, "Too many branches (38 > 10)", 38),
    finding("PLR0915", 1, "Too many statements (65 > 40)", 65),
    finding("CPX001", 1, "`shipping_cost` has cognitive complexity 62 > 15", 62),
    *[finding("FBT001", 2, "Boolean-typed positional argument in function definition") for _ in range(3)],
    *[finding("SIM108", line, f"Use ternary operator (case {line})") for line in (16, 25, 36, 45, 56, 65)],
]


def test_repeated_rules_collapse_to_one_line() -> None:
    report = format_report(LIVE_RUN, RUN)
    assert "shipping.py FBT001 x3 at line 2: Boolean-typed" in report
    assert "shipping.py SIM108 x6 at lines 16, 25, 36, 45, 56, 65: Use ternary operator (case 16)" in report
    assert report.count("SIM108") == 1


def test_each_hint_is_printed_once_with_its_rules() -> None:
    report = format_report(LIVE_RUN, RUN)
    assert report.count("flatten it") == 1
    assert "- C901, PLR0912, CPX001: flatten it" in report
    assert "- PLR0915: remove what the task does not need" in report


def test_size_and_complexity_findings_come_first() -> None:
    lines = format_report(LIVE_RUN, RUN).splitlines()
    assert lines[0].startswith("plainpy (standard): 13 new problem(s) in shipping.py")
    assert [line.split()[1] for line in lines[1:5]] == ["C901", "PLR0912", "PLR0915", "CPX001"]
    assert len(lines) == 10


def test_long_line_lists_are_cut_short() -> None:
    findings = [finding("SIM108", line, "Use ternary operator") for line in range(10, 100, 10)]
    report = format_report(findings, RUN)
    assert "x9 at lines 10, 20, 30, 40, 50, 60, ...:" in report
