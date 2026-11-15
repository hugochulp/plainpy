# expect: Loader.load, handle, parse, todo, version_of
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
