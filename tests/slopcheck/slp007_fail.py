# expect: Client.get, fetch
def fetch(url: str, timeout: float = 30, *, retries: int = 3) -> str:
    return f"{url} {timeout} {retries}"


class Client:
    def get(self, path: str, verbose: bool = False) -> str:
        return path.upper() if verbose else path


def main() -> None:
    print(fetch("a"), fetch("b", retries=1), Client().get("/"))
