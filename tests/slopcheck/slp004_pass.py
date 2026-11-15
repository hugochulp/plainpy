# expect:
import functools


def area(width: int, height: int) -> int:
    return width * height


def swapped(height: int, width: int) -> int:
    return area(width, height)


def square(side: int) -> int:
    return area(side, side)


def doubled(width: int) -> int:
    return area(width, 2)


@functools.cache
def cached(width: int, height: int) -> int:
    return area(width, height)


def main() -> None:
    print(area(1, 2))


def cleaned(text: str) -> str:
    return str(text.strip())


def legacy(width: int, height: int) -> int:  # slopcheck: allow SLP004 -- old name kept for callers
    return area(width, height)
