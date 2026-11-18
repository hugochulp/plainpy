# expect: CHECKS, OPS, Parser.compile, evaluate, register()
import operator

OPS = {
    "<": lambda a, b: a < b,
    "+": lambda a, b: a + b,
}

CHECKS = [
    ("name", lambda row: isinstance(row.get("name"), str)),
    ("age", lambda row: row.get("age", 0) >= 0),
]


def evaluate(op: str, a: int, b: int) -> object:
    table = {"-": lambda: a - b, "*": lambda: a * b if b else 0}
    return table[op]()


class Parser:
    def compile(self, kind: str) -> object:
        return (lambda s: s.strip() or None, lambda s: s.split(",") if s else [])[kind == "list"]


SAFE = {"<": operator.lt}


def register(table: object) -> None:
    del table


register({"neg": lambda x: -x, "not": lambda x: not x})
