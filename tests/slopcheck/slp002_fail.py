# expect: Doubler, Greeter, Paths
class Doubler:
    def __init__(self, factor: int) -> None:
        self.factor = factor

    def apply(self, value: int) -> int:
        return value * self.factor


class Paths:
    @staticmethod
    def home() -> str:
        return "~"

    @staticmethod
    def tmp() -> str:
        return "/tmp"


class Greeter:
    def greet(self, name: str) -> str:
        return f"Hello, {name}"
