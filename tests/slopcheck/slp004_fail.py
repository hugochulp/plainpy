# expect: Service.run, fetch, load
def read(path: str) -> str:
    return path


async def get(url: str) -> str:
    return url


def load(path: str) -> str:
    """Load a file."""
    return read(path)


async def fetch(url: str) -> str:
    return await get(url)


class Service:
    def __init__(self, engine: object) -> None:
        self.engine = engine

    def run(self, job: str, *, retries: int = 1) -> object:
        return self.engine.run(job, retries=retries)
