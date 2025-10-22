"""Core services implementing the bot's business logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from .config import FilterConfig, PricingConfig
from .filtering import WordFilter
from .models import GoldenCard, Post, User
from .storage import AbstractStorage

DEFAULT_REGISTRATION_ENERGY = 100
DEFAULT_REFERRAL_ENERGY = 50
POST_ENERGY_COST = 20


@dataclass(slots=True)
class ChannelEconomyService:
    storage: AbstractStorage
    pricing: PricingConfig
    filter_config: FilterConfig
    registration_energy: int = DEFAULT_REGISTRATION_ENERGY
    referral_energy: int = DEFAULT_REFERRAL_ENERGY
    post_energy_cost: int = POST_ENERGY_COST
    word_filter: WordFilter | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "word_filter",
            WordFilter.from_iterable(self.filter_config.banned_words),
        )

    def register_user(self, user_id: int, subscribed_to_sponsors: bool) -> User:
        if not subscribed_to_sponsors:
            raise ValueError("User must subscribe to sponsors before registering")
        user = self.storage.get_user(user_id)
        if user is None:
            user = User(user_id=user_id)
            user.add_energy(self.registration_energy)
            self.storage.save_user(user)
        return user

    def _get_or_create_user(self, user_id: int) -> User:
        user = self.storage.get_user(user_id)
        if user is None:
            user = User(user_id=user_id)
            self.storage.save_user(user)
        return user

    def purchase_energy(self, user_id: int, amount: int) -> float:
        user = self._get_or_create_user(user_id)
        price = self.pricing.price_for_energy(amount)
        user.add_energy(amount)
        self.storage.save_user(user)
        return price

    def award_referral(self, referrer_id: int, referred_id: int) -> None:
        referrer = self._get_or_create_user(referrer_id)
        referred = self._get_or_create_user(referred_id)
        if referred_id == referrer_id:
            raise ValueError("User cannot refer themselves")
        if referred_id in referrer.referred_users:
            return
        referrer.referred_users.add(referred_id)
        referrer.add_energy(self.referral_energy)
        self.storage.save_user(referrer)
        self.storage.save_user(referred)

    def submit_post(self, user_id: int, text: str) -> Post:
        user = self._get_or_create_user(user_id)
        self.word_filter.assert_allowed(text)
        user.spend_energy(self.post_energy_cost)
        golden_card = user.pop_active_golden_card()
        post = Post(user_id=user_id, text=text, requires_pin=golden_card is not None)
        self.storage.save_user(user)
        self.storage.add_post(post)
        return post

    def purchase_golden_card(self, user_id: int, duration: timedelta) -> float:
        if duration <= timedelta(0):
            raise ValueError("Golden card duration must be positive")
        user = self._get_or_create_user(user_id)
        price = self.pricing.price_for_golden_card(duration)
        user.add_golden_card(GoldenCard(duration=duration))
        self.storage.save_user(user)
        return price

    def get_user_balance(self, user_id: int) -> Optional[User]:
        return self.storage.get_user(user_id)
