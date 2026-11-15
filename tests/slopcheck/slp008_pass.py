# expect:
from abc import ABC, abstractmethod
from typing import Protocol, overload


class Store(ABC):
    @abstractmethod
    def save(self, item: str) -> None: ...


class Reader(Protocol):
    def read(self) -> str: ...


class Plugin:  # subclassed in the project: an empty method is a hook
    def setup(self) -> None:
        pass


class Mine(Plugin):
    def setup(self) -> None:
        pass


class Problem(Exception):
    pass


def enabled() -> bool:  # no parameters, so a constant is the real answer
    return True


@overload
def double(x: int) -> int: ...
@overload
def double(x: str) -> str: ...
def double(x: int | str) -> int | str:
    return x * 2


def __getattr__(name: str) -> None:
    return None
