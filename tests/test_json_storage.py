"""Tests for the JSON backed storage implementation."""

from __future__ import annotations

from datetime import timedelta

from channel_admin.models import BotSettings, GoldenCard, Invoice, Post, User, utcnow
from channel_admin.storage import JsonStorage


def test_json_storage_persists_between_sessions(tmp_path) -> None:
    db_path = tmp_path / "storage.json"

    storage = JsonStorage(db_path)

    user = User(user_id=1, energy=5, is_admin=True)
    user.referred_users.add(2)
    user.add_golden_card(GoldenCard(duration=timedelta(hours=12)))
    storage.save_user(user)

    post = Post(user_id=1, text="Hello")
    storage.add_post(post)
    post.status = "approved"
    storage.save_post(post)

    invoice = Invoice(
        invoice_id=100,
        user_id=1,
        invoice_type="energy",
        amount=50.0,
        asset="USDT",
        pay_url="https://example.com",
        price=5.0,
        status="paid",
        paid_at=utcnow(),
        energy_amount=100,
    )
    storage.save_invoice(invoice)

    settings = BotSettings(autopost_paused=True, post_energy_cost=40, energy_price_per_unit=1.5)
    storage.save_settings(settings)

    assert db_path.exists()

    fresh_storage = JsonStorage(db_path)

    loaded_user = fresh_storage.get_user(1)
    assert loaded_user is not None
    assert loaded_user.energy == 5
    assert loaded_user.is_admin is True
    assert 2 in loaded_user.referred_users
    assert len(loaded_user.golden_cards) == 1

    loaded_post = fresh_storage.get_post(post.post_id)
    assert loaded_post is not None
    assert loaded_post.status == "approved"

    loaded_invoice = fresh_storage.get_invoice(invoice.invoice_id)
    assert loaded_invoice is not None
    assert loaded_invoice.status == "paid"
    assert loaded_invoice.paid_at is not None
    assert loaded_invoice.energy_amount == 100

    loaded_settings = fresh_storage.get_settings()
    assert loaded_settings.autopost_paused is True
    assert loaded_settings.post_energy_cost == 40
    assert loaded_settings.energy_price_per_unit == 1.5

    new_post = Post(user_id=1, text="Another")
    fresh_storage.add_post(new_post)
    assert new_post.post_id == (post.post_id or 0) + 1
