"""Integration helpers for Crypto Pay invoices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp


def _extract_error_message(payload: dict[str, Any]) -> str | None:
    """Return the most helpful error message from a Crypto Pay response."""

    for key in ("error", "description", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None

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

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.api_base}/createInvoice",
                    headers=headers,
                    json=request_body,
                ) as response:
                    try:
                        payload_json = await response.json()
                    except aiohttp.ContentTypeError as exc:
                        text = await response.text()
                        raise CryptoPayError(
                            "Unexpected response from Crypto Pay: {text}".format(
                                text=text.strip() or response.reason or response.status
                            )
                        ) from exc
                    status = response.status
                    reason = response.reason
        except aiohttp.ClientError as exc:
            raise CryptoPayError(f"Failed to contact Crypto Pay: {exc}") from exc

        if status >= 400:
            message = _extract_error_message(payload_json)
            if message is None:
                message = reason or f"HTTP error {status}"
            raise CryptoPayError(message)

        if not payload_json.get("ok", False):
            message = _extract_error_message(payload_json) or "Crypto Pay request failed"
            raise CryptoPayError(message)

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

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.api_base}/getInvoices",
                    headers=headers,
                    json=request_body,
                ) as response:
                    try:
                        payload_json = await response.json()
                    except aiohttp.ContentTypeError as exc:
                        text = await response.text()
                        raise CryptoPayError(
                            "Unexpected response from Crypto Pay: {text}".format(
                                text=text.strip() or response.reason or response.status
                            )
                        ) from exc
                    status = response.status
                    reason = response.reason
        except aiohttp.ClientError as exc:
            raise CryptoPayError(f"Failed to contact Crypto Pay: {exc}") from exc

        if status >= 400:
            message = _extract_error_message(payload_json)
            if message is None:
                message = reason or f"HTTP error {status}"
            raise CryptoPayError(message)

        if not payload_json.get("ok", False):
            message = _extract_error_message(payload_json) or "Crypto Pay request failed"
            raise CryptoPayError(message)

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
