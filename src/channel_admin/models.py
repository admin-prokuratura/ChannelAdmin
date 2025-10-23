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
    is_banned: bool = False
    is_admin: bool = False
    username: str | None = None
    full_name: str | None = None

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
    post_id: int | None = None
    status: str = "pending"
    channel_message_id: Optional[int] = None
    chat_message_id: Optional[int] = None
    button_text: Optional[str] = None
    button_url: Optional[str] = None
    photo_file_id: Optional[str] = None
    parse_mode: Optional[str] = None


@dataclass(slots=True)
class GoldenCard:
    duration: timedelta
    purchased_at: datetime = field(default_factory=utcnow)

    @property
    def expires_at(self) -> datetime:
        return self.purchased_at + self.duration


@dataclass(slots=True)
class Invoice:
    invoice_id: int
    user_id: int
    invoice_type: str
    amount: float
    asset: str
    pay_url: str
    price: float
    status: str = "pending"
    created_at: datetime = field(default_factory=utcnow)
    paid_at: Optional[datetime] = None
    payload: Optional[str] = None
    energy_amount: Optional[int] = None
    golden_hours: Optional[int] = None


@dataclass(slots=True)
class TicketMessage:
    message_id: int
    ticket_id: int
    sender: str
    text: str
    created_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class Ticket:
    ticket_id: int
    user_id: int
    status: str = "open"
    subject: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    messages: list[TicketMessage] = field(default_factory=list)

    def add_message(self, message: TicketMessage) -> None:
        self.messages.append(message)
        self.updated_at = message.created_at
        if not self.subject and message.sender == "user":
            preview = message.text.strip()
            self.subject = preview[:80] if preview else None


@dataclass(slots=True)
class BotSettings:
    autopost_paused: bool = False
    post_energy_cost: int = 20
    energy_price_per_unit: float = 1.0
    subscription_chat_id: str | None = None
    subscription_invite_link: str | None = None
