"""Integration helpers for Crypto Pay invoices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp

API_BASE_URL = "https://pay.crypt.bot/api"


class CryptoPayError(RuntimeError):
    """Raised when Crypto Pay API returns an error response."""


@dataclass(slots=True)
class CryptoPayInvoice:
    """Represents a Crypto Pay invoice."""

    invoice_id: int
    pay_url: str
    amount: float
    asset: str
    status: str
    description: str | None = None
    payload: str | None = None


@dataclass(slots=True)
class CryptoPayClient:
    """Minimal async client for interacting with the Crypto Pay API."""

    token: str
    default_asset: str = "USDT"
    api_base: str = API_BASE_URL

    async def create_invoice(
        self,
        *,
        amount: float,
        description: str,
        payload: str | None = None,
        asset: str | None = None,
    ) -> CryptoPayInvoice:
        """Create a payment invoice and return its public link."""

        if amount <= 0:
            raise ValueError("Amount must be positive")

        request_body: dict[str, Any] = {
            "amount": round(amount, 2),
            "asset": asset or self.default_asset,
            "description": description,
        }
        if payload is not None:
            request_body["payload"] = payload

        headers = {"Crypto-Pay-API-Token": self.token}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.api_base}/createInvoice",
                headers=headers,
                json=request_body,
            ) as response:
                response.raise_for_status()
                payload_json = await response.json()

        if not payload_json.get("ok", False):
            raise CryptoPayError(payload_json.get("error", "Crypto Pay request failed"))

        result = payload_json["result"]
        return CryptoPayInvoice(
            invoice_id=int(result["invoice_id"]),
            pay_url=str(result["pay_url"]),
            amount=float(result["amount"]),
            asset=str(result["asset"]),
            status=str(result["status"]),
            description=result.get("description"),
            payload=result.get("payload"),
        )

    async def get_invoice(self, invoice_id: int) -> CryptoPayInvoice:
        """Retrieve a single invoice by id."""

        headers = {"Crypto-Pay-API-Token": self.token}
        request_body = {"invoice_ids": [invoice_id]}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.api_base}/getInvoices",
                headers=headers,
                json=request_body,
            ) as response:
                response.raise_for_status()
                payload_json = await response.json()

        if not payload_json.get("ok", False):
            raise CryptoPayError(payload_json.get("error", "Crypto Pay request failed"))

        raw_results = payload_json.get("result") or []
        if isinstance(raw_results, dict):
            results = list(raw_results.values())
        elif isinstance(raw_results, list):
            results = raw_results
        else:
            results = []

        if not results:
            raise CryptoPayError(f"Invoice {invoice_id} not found")

        result = results[0]
        return CryptoPayInvoice(
            invoice_id=int(result["invoice_id"]),
            pay_url=str(result["pay_url"]),
            amount=float(result["amount"]),
            asset=str(result["asset"]),
            status=str(result["status"]),
            description=result.get("description"),
            payload=result.get("payload"),
        )
