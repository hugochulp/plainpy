# expect: Loader.load, handle, parse, run.<locals>.step, todo, version_of
def todo():
    pass


def parse(text: str) -> dict[str, str]: ...


def handle(event: str) -> None:
    """Handle the event."""


class Loader:
    def load(self, path: str) -> bytes:
        raise NotImplementedError("later")


def version_of(package: str) -> str:
    return "1.0"


def run() -> None:
    def step() -> None:
        pass

    step()
