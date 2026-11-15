import secrets
from abc import ABC, abstractmethod


class Vault(ABC):
    @abstractmethod
    def store(self, kind: str, value: str) -> str: ...


class FakeVault(Vault):
    def __init__(self):
        self._count = 0

    def store(self, kind, value):
        if kind != "email" or not isinstance(value, str) or len(value) > 254:
            raise ValueError("Invalid vault value.")
        self._count += 1
        return "vault-" + secrets.token_urlsafe(8)
