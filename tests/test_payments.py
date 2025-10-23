"""Tests for CryptoPayClient invoice retrieval."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from channel_admin.payments import CryptoPayClient, CryptoPayError


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200, reason: str = "OK"):
        self._payload = payload
        self.status = status
        self.reason = reason

    async def json(self) -> dict:
        return self._payload

    async def text(self) -> str:
        return json.dumps(self._payload)


class _FakeRequestContext:
    def __init__(self, payload: dict, status: int = 200, reason: str = "OK"):
        self._payload = payload
        self._status = status
        self._reason = reason

    async def __aenter__(self) -> _FakeResponse:
        return _FakeResponse(self._payload, status=self._status, reason=self._reason)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeClientSession:
    def __init__(self, payload: dict, status: int = 200, reason: str = "OK"):
        self._payload = payload
        self._status = status
        self._reason = reason

    async def __aenter__(self) -> "_FakeClientSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, *args, **kwargs) -> _FakeRequestContext:
        return _FakeRequestContext(self._payload, status=self._status, reason=self._reason)


def _patch_session(
    monkeypatch: pytest.MonkeyPatch, payload: dict, *, status: int = 200, reason: str = "OK"
) -> None:
    monkeypatch.setattr(
        "channel_admin.payments.aiohttp.ClientSession",
        lambda: _FakeClientSession(payload, status=status, reason=reason),
    )


def test_get_invoice_accepts_list_result(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "ok": True,
        "result": [
            {
                "invoice_id": "101",
                "pay_url": "https://pay.crypt.bot/invoice/101",
                "amount": "5.50",
                "asset": "USDT",
                "status": "paid",
            }
        ],
    }
    _patch_session(monkeypatch, payload)

    client = CryptoPayClient(token="token")
    invoice = asyncio.run(client.get_invoice(101))

    assert invoice.invoice_id == 101
    assert invoice.pay_url.endswith("/101")
    assert invoice.amount == 5.50
    assert invoice.asset == "USDT"
    assert invoice.status == "paid"


def test_get_invoice_accepts_dict_result(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "ok": True,
        "result": {
            "101": {
                "invoice_id": "101",
                "pay_url": "https://pay.crypt.bot/invoice/101",
                "amount": "5.50",
                "asset": "USDT",
                "status": "active",
            }
        },
    }
    _patch_session(monkeypatch, payload)

    client = CryptoPayClient(token="token")
    invoice = asyncio.run(client.get_invoice(101))

    assert invoice.invoice_id == 101
    assert invoice.status == "active"


def test_get_invoice_raises_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"ok": True, "result": []}
    _patch_session(monkeypatch, payload)

    client = CryptoPayClient(token="token")

    with pytest.raises(CryptoPayError):
        asyncio.run(client.get_invoice(999))


def test_create_invoice_raises_for_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"ok": False, "error": "Unauthorized"}
    _patch_session(monkeypatch, payload, status=401, reason="Unauthorized")

    client = CryptoPayClient(token="bad-token")

    with pytest.raises(CryptoPayError) as excinfo:
        asyncio.run(
            client.create_invoice(amount=10.0, description="Test invoice", payload="energy:1:10")
        )

    assert "Unauthorized" in str(excinfo.value)


def test_create_invoice_wraps_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FailingSession:
        async def __aenter__(self):
            raise aiohttp.ClientError("Connection reset")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr("channel_admin.payments.aiohttp.ClientSession", _FailingSession)

    client = CryptoPayClient(token="token")

    with pytest.raises(CryptoPayError) as excinfo:
        asyncio.run(client.create_invoice(amount=10.0, description="Test"))

    assert "Failed to contact Crypto Pay" in str(excinfo.value)
