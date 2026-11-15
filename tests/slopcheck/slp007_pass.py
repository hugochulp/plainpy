# expect:
from collections.abc import Callable


def fetch(url: str, timeout: float = 30) -> str:
    return f"{url} {timeout}"


def send(message: str, *, urgent: bool = False) -> str:
    return message.upper() if urgent else message


def _pad(text: str, width: int = 10) -> str:  # private
    return text.ljust(width)


def spread(a: int = 1, b: int = 2) -> int:  # called with **
    return a + b


def on_event(name: str = "tick") -> str:  # passed around as a value
    return name


def never_called(flag: bool = False) -> bool:  # no call site to judge by
    return flag


class Base:  # subclassed: its methods may be overridden with the same options
    def run(self, fast: bool = False) -> bool:
        return fast


class Quick(Base):
    def run(self, fast: bool = True) -> bool:
        return not fast


def main(handlers: list[Callable[[], str]]) -> None:
    options = {"a": 2}
    handlers.append(on_event)
    print(fetch("a", 5), send("b", urgent=True), _pad("c"), spread(**options), on_event(), Base().run(), Quick().run())
