# expect: Notifier, Store
from abc import ABC, abstractmethod
from typing import Protocol


class Store(ABC):
    @abstractmethod
    def save(self, item: str) -> None: ...


class FileStore(Store):
    def save(self, item: str) -> None:
        print(item)


class Notifier(Protocol):
    def notify(self, text: str) -> None: ...


class EmailNotifier:
    def notify(self, text: str) -> None:
        print(text)

    def close(self) -> None:
        print("closed")
