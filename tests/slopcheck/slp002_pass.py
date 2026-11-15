# expect:
import enum
from dataclasses import dataclass


@dataclass
class Point:
    x: int

    def moved(self) -> "Point":
        return Point(self.x + 1)


class Counter:
    def __init__(self) -> None:
        self.count = 0

    def add(self) -> None:
        self.count += 1

    def reset(self) -> None:
        self.count = 0


class Colour(enum.Enum):
    RED = 1


class Timer:
    def __enter__(self) -> "Timer":
        return self

    def __exit__(self, *_: object) -> None:
        print("done")

    def elapsed(self) -> float:
        return 0.0


class Settings:
    def __init__(self, name: str) -> None:
        self.name = name


class Legacy:  # slopcheck: allow SLP002 -- public API kept for existing callers
    def run(self) -> None:
        print("run")
