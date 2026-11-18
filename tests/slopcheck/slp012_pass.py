# expect:
import operator

OPS = {"<": operator.lt, "+": operator.add}
SORT_KEYS = {"name": lambda row: row.name.lower(), "size": lambda row: len(row["data"]), "none": lambda row: None}
DEFAULTS = {"tags": lambda: [], "meta": lambda: {}}
ONE = {"double": lambda x: x * 2, "label": "x"}


def pick(rows: list[dict[str, int]]) -> list[dict[str, int]]:
    return sorted(rows, key=lambda row: row["a"] + row["b"])


def legacy(op: str) -> object:  # slopcheck: allow SLP012 -- mirrors the vendor's callback table
    return {"a": lambda x: x + 1, "b": lambda x: x - 1}[op]


RULES = {  # slopcheck: allow SLP012 -- plugin registry the framework iterates
    "inc": lambda x: x + 1,
    "dec": lambda x: x - 1,
}
