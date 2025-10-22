"""Tests for CryptoPayClient invoice retrieval."""

from __future__ import annotations

import asyncio

import pytest

from channel_admin.payments import CryptoPayClient, CryptoPayError


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:  # pragma: no cover - nothing to raise here
        return None

    async def json(self) -> dict:
        return self._payload


class _FakeRequestContext:
    def __init__(self, payload: dict):
        self._payload = payload

    async def __aenter__(self) -> _FakeResponse:
        return _FakeResponse(self._payload)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeClientSession:
    def __init__(self, payload: dict):
        self._payload = payload

    async def __aenter__(self) -> "_FakeClientSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, *args, **kwargs) -> _FakeRequestContext:
        return _FakeRequestContext(self._payload)


def _patch_session(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    monkeypatch.setattr(
        "channel_admin.payments.aiohttp.ClientSession",
        lambda: _FakeClientSession(payload),
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
