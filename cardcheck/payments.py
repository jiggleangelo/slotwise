from abc import ABC, abstractmethod


class PaymentProvider(ABC):
    @abstractmethod
    def checkout_url(self, sku: str) -> str: ...

    @abstractmethod
    def verify_webhook(self, body: bytes, headers: dict) -> bool: ...


class FakePayments(PaymentProvider):
    def checkout_url(self, sku):
        return "/pricing?clicked=" + ("five" if sku == "five" else "single")

    def verify_webhook(self, body, headers):
        return False
