# expect:
def _double(value: int) -> int:
    return value * 2


def _normalised(text: str) -> str:
    text = text.strip()
    text = text.lower()
    return text.replace(" ", "-")


def helper(value: int) -> int:
    return value + 1


def total(value: int) -> int:
    return _double(value) + _double(1) + helper(value)


def slug(text: str) -> str:
    return _normalised(text)
