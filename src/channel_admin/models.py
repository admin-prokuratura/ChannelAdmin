"""Domain models for the Channel Admin bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class User:
    user_id: int
    energy: int = 0
    golden_cards: list["GoldenCard"] = field(default_factory=list)
    referred_users: set[int] = field(default_factory=set)

    def add_energy(self, amount: int) -> None:
        if amount < 0:
            raise ValueError("Cannot add negative energy")
        self.energy += amount

    def spend_energy(self, amount: int) -> None:
        if amount <= 0:
            raise ValueError("Energy spend must be positive")
        if self.energy < amount:
            raise ValueError("Not enough energy")
        self.energy -= amount

    def add_golden_card(self, golden_card: "GoldenCard") -> None:
        self.golden_cards.append(golden_card)

    def pop_active_golden_card(self) -> Optional["GoldenCard"]:
        now = utcnow()
        for index, card in enumerate(self.golden_cards):
            if card.expires_at > now:
                return self.golden_cards.pop(index)
        return None


@dataclass(slots=True)
class Post:
    user_id: int
    text: str
    requires_pin: bool = False
    created_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class GoldenCard:
    duration: timedelta
    purchased_at: datetime = field(default_factory=utcnow)

    @property
    def expires_at(self) -> datetime:
        return self.purchased_at + self.duration
