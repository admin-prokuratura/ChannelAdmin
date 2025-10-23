from datetime import timedelta

import pytest

from channel_admin.config import FilterConfig, PricingConfig
from channel_admin.models import UserboxProfile
from channel_admin.services import ChannelEconomyService, ChimeraService
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


@pytest.fixture()
def chimera_service() -> ChimeraService:
    return ChimeraService(storage=InMemoryStorage())


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


def test_purchase_golden_card_with_energy(service: ChannelEconomyService) -> None:
    service.purchase_energy(1, 200)
    cost = service.energy_cost_for_golden_card(timedelta(hours=24))
    assert cost is not None
    spent = service.purchase_golden_card_with_energy(1, timedelta(hours=24))
    assert spent == cost
    user = service.get_user_balance(1)
    assert user is not None
    assert len(user.golden_cards) == 1
    assert user.energy == 200 - cost


def test_award_referral_only_once(service: ChannelEconomyService) -> None:
    service.award_referral(1, 2)
    service.award_referral(1, 2)
    user = service.get_user_balance(1)
    assert user and user.energy == service.referral_energy


def test_support_ticket_flow(service: ChannelEconomyService) -> None:
    service.register_user(1, subscribed_to_sponsors=True)
    ticket = service.open_ticket(1, "Нужна помощь")
    assert ticket.ticket_id > 0
    assert ticket.status == "open"
    assert len(ticket.messages) == 1

    updated = service.add_ticket_message(ticket.ticket_id, "admin", "Добрый день")
    assert len(updated.messages) == 2

    service.close_ticket(ticket.ticket_id)
    closed = service.get_ticket(ticket.ticket_id)
    assert closed is not None
    assert closed.status == "closed"

    reopened = service.reopen_ticket(ticket.ticket_id)
    assert reopened.status == "open"

    user_tickets = service.list_user_tickets(1)
    assert len(user_tickets) == 1
    assert user_tickets[0].ticket_id == ticket.ticket_id


def test_chimera_service_records_address_search(
    chimera_service: ChimeraService,
) -> None:
    record = chimera_service.record_address_search(
        "Москва, Тверская 1",
        results=[{"apartment": "42"}, {"floor": 3}],
    )
    assert record.record_id is not None
    stored = chimera_service.get_record(record.record_id or 0)
    assert stored is not None
    assert stored.address_query == "Москва, Тверская 1"
    assert stored.raw_results[0]["apartment"] == "42"

    updated = chimera_service.attach_userbox_profile(
        record.record_id or 0,
        full_name="Иван Иванов",
        birth_date="01.01.1990",
        phone_numbers=["+79995553322", ""],
        address="Москва",
    )
    assert updated.userbox_profile == UserboxProfile(
        full_name="Иван Иванов",
        birth_date="01.01.1990",
        phone_numbers=["+79995553322"],
        address="Москва",
    )


def test_chimera_service_requires_address(
    chimera_service: ChimeraService,
) -> None:
    with pytest.raises(ValueError):
        chimera_service.record_address_search("   ")


def test_chimera_service_missing_record(
    chimera_service: ChimeraService,
) -> None:
    with pytest.raises(KeyError):
        chimera_service.attach_userbox_profile(999, full_name=None, birth_date=None)
