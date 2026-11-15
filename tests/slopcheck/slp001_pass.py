# expect:
from abc import ABC, abstractmethod
from typing import Protocol


class Store(ABC):
    @abstractmethod
    def save(self, item: str) -> None: ...


class FileStore(Store):
    def save(self, item: str) -> None:
        print(item)


class MemoryStore(Store):
    def save(self, item: str) -> None:
        self.items = [item]


class Readable(Protocol):  # describes objects from outside the project, e.g. open files
    def read(self) -> str: ...


class Pinger(Protocol):  # slopcheck: allow SLP001 -- second implementation lands next sprint
    def ping(self) -> None: ...


class Server:
    def ping(self) -> None:
        print("pong")

    def stop(self) -> None:
        print("stopped")
