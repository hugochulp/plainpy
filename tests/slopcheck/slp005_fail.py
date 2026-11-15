# expect: _double
def _double(value: int) -> int:
    return value * 2


def total(value: int) -> int:
    return _double(value) + 1
