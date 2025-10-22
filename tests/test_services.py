from datetime import timedelta

import pytest

from channel_admin.config import FilterConfig, PricingConfig
from channel_admin.services import ChannelEconomyService
from channel_admin.storage import InMemoryStorage


@pytest.fixture()
def service() -> ChannelEconomyService:
    pricing = PricingConfig(rubles_per_usd=1.0)
    service = ChannelEconomyService(
        storage=InMemoryStorage(),
        pricing=pricing,
        filter_config=FilterConfig(banned_words={"запрещено"}),
        registration_energy=100,
        referral_energy=30,
        post_energy_cost=10,
    )
    service.update_post_price(10)
    return service


def test_registration_awards_energy(service: ChannelEconomyService) -> None:
    user = service.register_user(1, subscribed_to_sponsors=True)
    assert user.energy == 100


def test_registration_stores_profile(service: ChannelEconomyService) -> None:
    user = service.register_user(
        1,
        subscribed_to_sponsors=True,
        username="alice",
        full_name="Alice Example",
    )
    assert user.username == "alice"
    assert user.full_name == "Alice Example"

    updated = service.register_user(
        1,
        subscribed_to_sponsors=True,
        username="alice_new",
        full_name="Alice Updated",
    )
    assert updated.username == "alice_new"
    assert updated.full_name == "Alice Updated"


def test_registration_requires_subscription(service: ChannelEconomyService) -> None:
    with pytest.raises(ValueError):
        service.register_user(1, subscribed_to_sponsors=False)


def test_purchase_energy_adds_balance(service: ChannelEconomyService) -> None:
    cost = service.purchase_energy(1, 50)
    assert pytest.approx(cost, rel=1e-3) == service.pricing.price_for_energy(50)
    user = service.get_user_balance(1)
    assert user and user.energy == 50


def test_filter_blocks_banned_words(service: ChannelEconomyService) -> None:
    service.purchase_energy(1, 50)
    with pytest.raises(ValueError):
        service.submit_post(1, "Это запрещено к публикации")


def test_post_spends_energy(service: ChannelEconomyService) -> None:
    service.purchase_energy(1, 50)
    post = service.submit_post(1, "Новый пост без фильтра")
    user = service.get_user_balance(1)
    assert user and user.energy == 40
    assert not post.requires_pin


def test_golden_card_makes_post_pin(service: ChannelEconomyService) -> None:
    service.purchase_energy(1, 50)
    service.purchase_golden_card(1, timedelta(hours=24))
    post = service.submit_post(1, "Пост с закрепом")
    assert post.requires_pin


def test_award_referral_only_once(service: ChannelEconomyService) -> None:
    service.award_referral(1, 2)
    service.award_referral(1, 2)
    user = service.get_user_balance(1)
    assert user and user.energy == service.referral_energy
