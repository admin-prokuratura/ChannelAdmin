"""Core services implementing the bot's business logic."""

from __future__ import annotations

import math

from dataclasses import dataclass
from datetime import timedelta
from typing import Dict, Iterable, Optional, Sequence

from .config import FilterConfig, PricingConfig
from .filtering import WordFilter
from .models import BotSettings, GoldenCard, Invoice, Post, Ticket, User, utcnow
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
    current_settings: BotSettings | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "word_filter",
            WordFilter.from_iterable(self.filter_config.banned_words),
        )
        self.apply_settings(self.storage.get_settings())

    def apply_settings(self, settings: BotSettings) -> None:
        self.post_energy_cost = settings.post_energy_cost
        usd_per_unit = self.pricing.convert_rub_to_usd(settings.energy_price_per_unit)
        self.pricing.energy_price_per_unit = usd_per_unit
        for amount in list(self.pricing.energy_bundle_prices.keys()):
            self.pricing.energy_bundle_prices[amount] = round(usd_per_unit * amount, 2)
        self.current_settings = settings

    def register_user(
        self,
        user_id: int,
        subscribed_to_sponsors: bool,
        *,
        username: str | None = None,
        full_name: str | None = None,
    ) -> User:
        if not subscribed_to_sponsors:
            raise ValueError("User must subscribe to sponsors before registering")
        user = self.storage.get_user(user_id)
        if user is None:
            user = User(user_id=user_id, username=username, full_name=full_name)
            user.add_energy(self.registration_energy)
            self.storage.save_user(user)
            return user
        updated = False
        if username is not None and username != user.username:
            user.username = username
            updated = True
        if full_name is not None and full_name != user.full_name:
            user.full_name = full_name
            updated = True
        if updated:
            self.storage.save_user(user)
        return user

    def _get_or_create_user(
        self,
        user_id: int,
        *,
        username: str | None = None,
        full_name: str | None = None,
    ) -> User:
        user = self.storage.get_user(user_id)
        if user is None:
            user = User(user_id=user_id, username=username, full_name=full_name)
            self.storage.save_user(user)
            return user
        updated = False
        if username is not None and username != user.username:
            user.username = username
            updated = True
        if full_name is not None and full_name != user.full_name:
            user.full_name = full_name
            updated = True
        if updated:
            self.storage.save_user(user)
        return user

    def update_user_profile(
        self,
        user_id: int,
        *,
        username: str | None = None,
        full_name: str | None = None,
    ) -> User:
        return self._get_or_create_user(
            user_id, username=username, full_name=full_name
        )

    def purchase_energy(self, user_id: int, amount: int) -> float:
        user = self._get_or_create_user(user_id)
        price = self.pricing.price_for_energy(amount)
        if user.is_banned:
            raise ValueError("Пользователь заблокирован")
        user.add_energy(amount)
        self.storage.save_user(user)
        return price

    def credit_energy(self, user_id: int, amount: int) -> User:
        if amount <= 0:
            raise ValueError("Energy amount must be positive")
        user = self._get_or_create_user(user_id)
        if user.is_banned:
            raise ValueError("Пользователь заблокирован")
        user.add_energy(amount)
        self.storage.save_user(user)
        return user

    def award_referral(self, referrer_id: int, referred_id: int) -> None:
        referrer = self._get_or_create_user(referrer_id)
        referred = self._get_or_create_user(referred_id)
        if referrer.is_banned:
            raise ValueError("Пользователь заблокирован")
        if referred_id == referrer_id:
            raise ValueError("User cannot refer themselves")
        if referred_id in referrer.referred_users:
            return
        referrer.referred_users.add(referred_id)
        referrer.add_energy(self.referral_energy)
        self.storage.save_user(referrer)
        self.storage.save_user(referred)

    def submit_post(
        self,
        user_id: int,
        text: str,
        *,
        button_text: str | None = None,
        button_url: str | None = None,
        photo_file_id: str | None = None,
        parse_mode: str | None = None,
    ) -> Post:
        user = self._get_or_create_user(user_id)
        if user.is_banned:
            raise ValueError("Пользователь заблокирован")
        self.word_filter.assert_allowed(text)
        user.spend_energy(self.post_energy_cost)
        golden_card = user.pop_active_golden_card()
        post = Post(
            user_id=user_id,
            text=text,
            requires_pin=golden_card is not None,
            button_text=button_text,
            button_url=button_url,
            photo_file_id=photo_file_id,
            parse_mode=parse_mode,
        )
        self.storage.save_user(user)
        self.storage.add_post(post)
        return post

    def energy_cost_for_golden_card(self, duration: timedelta) -> int | None:
        if duration <= timedelta(0):
            raise ValueError("Golden card duration must be positive")
        unit_price = self.pricing.energy_price_per_unit
        if unit_price <= 0:
            return None
        price = self.pricing.price_for_golden_card(duration)
        return max(1, math.ceil(price / unit_price))

    def purchase_golden_card(self, user_id: int, duration: timedelta) -> float:
        if duration <= timedelta(0):
            raise ValueError("Golden card duration must be positive")
        user = self._get_or_create_user(user_id)
        if user.is_banned:
            raise ValueError("Пользователь заблокирован")
        price = self.pricing.price_for_golden_card(duration)
        user.add_golden_card(GoldenCard(duration=duration))
        self.storage.save_user(user)
        return price

    def purchase_golden_card_with_energy(self, user_id: int, duration: timedelta) -> int:
        energy_cost = self.energy_cost_for_golden_card(duration)
        if energy_cost is None:
            raise ValueError("Оплата энергией недоступна")
        user = self._get_or_create_user(user_id)
        if user.is_banned:
            raise ValueError("Пользователь заблокирован")
        user.spend_energy(energy_cost)
        user.add_golden_card(GoldenCard(duration=duration))
        self.storage.save_user(user)
        return energy_cost

    def grant_golden_card(self, user_id: int, duration: timedelta) -> User:
        if duration <= timedelta(0):
            raise ValueError("Golden card duration must be positive")
        user = self._get_or_create_user(user_id)
        if user.is_banned:
            raise ValueError("Пользователь заблокирован")
        user.add_golden_card(GoldenCard(duration=duration))
        self.storage.save_user(user)
        return user

    def get_user_balance(self, user_id: int) -> Optional[User]:
        return self.storage.get_user(user_id)

    def record_invoice(self, invoice: Invoice) -> None:
        self.storage.save_invoice(invoice)

    def get_invoice(self, invoice_id: int) -> Optional[Invoice]:
        return self.storage.get_invoice(invoice_id)

    def mark_invoice_paid(self, invoice_id: int) -> Optional[Invoice]:
        invoice = self.storage.get_invoice(invoice_id)
        if not invoice:
            return None
        invoice.status = "paid"
        invoice.paid_at = invoice.paid_at or utcnow()
        self.storage.save_invoice(invoice)
        return invoice

    def list_invoices_for_user(self, user_id: int) -> list[Invoice]:
        return list(self.storage.list_invoices_for_user(user_id))

    def open_ticket(self, user_id: int, message: str) -> Ticket:
        text = (message or "").strip()
        if not text:
            raise ValueError("Сообщение не должно быть пустым")
        user = self._get_or_create_user(user_id)
        if user.is_banned:
            raise ValueError("Пользователь заблокирован")
        return self.storage.create_ticket(user_id, text)

    def add_ticket_message(
        self, ticket_id: int, sender: str, message: str
    ) -> Ticket:
        text = (message or "").strip()
        if not text:
            raise ValueError("Сообщение не должно быть пустым")
        if sender not in {"user", "admin"}:
            raise ValueError("Недопустимый отправитель")
        ticket = self.storage.add_ticket_message(ticket_id, sender, text)
        if ticket is None:
            raise ValueError("Тикет не найден")
        return ticket

    def get_ticket(self, ticket_id: int) -> Ticket | None:
        return self.storage.get_ticket(ticket_id)

    def list_user_tickets(self, user_id: int) -> list[Ticket]:
        return list(self.storage.list_tickets_for_user(user_id))

    def list_tickets(self, status: str | None = None) -> list[Ticket]:
        return list(self.storage.list_tickets(status=status))

    def close_ticket(
        self, ticket_id: int, *, actor_user_id: int | None = None
    ) -> Ticket:
        ticket = self.storage.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError("Тикет не найден")
        if actor_user_id is not None and ticket.user_id != actor_user_id:
            raise PermissionError("Недостаточно прав для изменения тикета")
        ticket.status = "closed"
        ticket.updated_at = utcnow()
        self.storage.save_ticket(ticket)
        return ticket

    def reopen_ticket(
        self, ticket_id: int, *, actor_user_id: int | None = None
    ) -> Ticket:
        ticket = self.storage.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError("Тикет не найден")
        if actor_user_id is not None and ticket.user_id != actor_user_id:
            raise PermissionError("Недостаточно прав для изменения тикета")
        ticket.status = "open"
        ticket.updated_at = utcnow()
        self.storage.save_ticket(ticket)
        return ticket

    def set_autopost_paused(self, paused: bool) -> None:
        settings = self.storage.get_settings()
        settings.autopost_paused = paused
        self.storage.save_settings(settings)

    def is_autopost_paused(self) -> bool:
        return self.storage.get_settings().autopost_paused

    def get_statistics(self) -> Dict[str, int]:
        pending_posts = self.storage.count_posts(status="pending")
        published_posts = self.storage.count_posts(status="published")
        total_posts = self.storage.count_posts()
        return {
            "users": self.storage.count_users(),
            "posts_total": total_posts,
            "posts_pending": pending_posts,
            "posts_published": published_posts,
        }

    def list_pending_posts(self) -> list[Post]:
        return list(self.storage.list_posts_by_status("pending"))

    def get_finance_summary(self) -> Dict[str, float]:
        invoices = list(self.storage.list_invoices())
        total = len(invoices)
        paid = sum(1 for inv in invoices if inv.status == "paid")
        pending = total - paid
        collected = sum(inv.price for inv in invoices if inv.status == "paid")
        awaiting = sum(inv.price for inv in invoices if inv.status != "paid")
        return {
            "invoices_total": total,
            "invoices_paid": paid,
            "invoices_pending": pending,
            "revenue_collected": collected,
            "revenue_waiting": awaiting,
        }

    def reserve_next_post(self) -> Optional[Post]:
        for post in self.storage.list_posts_by_status("approved"):
            user = self.storage.get_user(post.user_id)
            if user and user.is_banned:
                post.status = "cancelled"
                self.storage.save_post(post)
                continue
            if post.status == "approved":
                post.status = "publishing"
                self.storage.save_post(post)
                return post
        return None

    def mark_post_published(
        self,
        post_id: int,
        *,
        channel_message_id: Optional[int],
        chat_message_id: Optional[int],
    ) -> Optional[Post]:
        post = self.storage.get_post(post_id)
        if not post:
            return None
        post.status = "published"
        post.channel_message_id = channel_message_id
        post.chat_message_id = chat_message_id
        self.storage.save_post(post)
        return post

    def mark_post_failed(self, post_id: int) -> Optional[Post]:
        post = self.storage.get_post(post_id)
        if not post:
            return None
        post.status = "approved"
        self.storage.save_post(post)
        return post

    def update_post_parse_mode(
        self, post_id: int, parse_mode: Optional[str]
    ) -> Optional[Post]:
        post = self.storage.get_post(post_id)
        if not post:
            return None
        post.parse_mode = parse_mode
        self.storage.save_post(post)
        return post

    def get_settings(self) -> BotSettings:
        return self.storage.get_settings()

    def list_users(self) -> list[User]:
        return list(self.storage.list_users())

    def update_post_price(self, cost: int) -> BotSettings:
        if cost <= 0:
            raise ValueError("Стоимость поста должна быть положительной")
        settings = self.storage.get_settings()
        settings.post_energy_cost = cost
        self.storage.save_settings(settings)
        self.apply_settings(settings)
        return settings

    def set_user_energy(self, user_id: int, energy: int) -> User:
        if energy < 0:
            raise ValueError("Баланс не может быть отрицательным")
        user = self._get_or_create_user(user_id)
        user.energy = energy
        self.storage.save_user(user)
        return user

    def adjust_user_energy(self, user_id: int, delta: int) -> User:
        user = self._get_or_create_user(user_id)
        if user.energy + delta < 0:
            raise ValueError("Результирующий баланс не может быть отрицательным")
        user.energy += delta
        self.storage.save_user(user)
        return user

    def set_user_admin(self, user_id: int, is_admin: bool) -> User:
        user = self._get_or_create_user(user_id)
        user.is_admin = is_admin
        self.storage.save_user(user)
        return user

    def set_user_banned(self, user_id: int, is_banned: bool) -> User:
        user = self._get_or_create_user(user_id)
        user.is_banned = is_banned
        self.storage.save_user(user)
        return user

    def list_posts_for_user(
        self, user_id: int, statuses: Optional[Sequence[str]] = None
    ) -> list[Post]:
        status_set = set(statuses) if statuses is not None else None
        return list(self.storage.list_posts_for_user(user_id, status_set))

    def cancel_posts_for_user(self, user_id: int) -> int:
        posts = self.storage.list_posts_for_user(user_id, {"pending", "approved", "publishing"})
        count = 0
        for post in posts:
            post.status = "cancelled"
            self.storage.save_post(post)
            count += 1
        return count

    def approve_post(self, post_id: int) -> Optional[Post]:
        post = self.storage.get_post(post_id)
        if not post:
            return None
        user = self.storage.get_user(post.user_id)
        if user and user.is_banned:
            post.status = "cancelled"
            self.storage.save_post(post)
            return None
        post.status = "approved"
        self.storage.save_post(post)
        return post

    def reject_post(self, post_id: int) -> Optional[Post]:
        post = self.storage.get_post(post_id)
        if not post:
            return None
        if post.status == "rejected":
            return post
        post.status = "rejected"
        self.storage.save_post(post)
        user = self._get_or_create_user(post.user_id)
        user.add_energy(self.post_energy_cost)
        self.storage.save_user(user)
        return post

    def update_energy_price(self, price: float) -> BotSettings:
        if price <= 0:
            raise ValueError("Цена за энергию должна быть положительной")
        settings = self.storage.get_settings()
        settings.energy_price_per_unit = price
        self.storage.save_settings(settings)
        self.apply_settings(settings)
        return settings

    def update_subscription_requirement(
        self, chat_id: str | None, invite_link: str | None
    ) -> BotSettings:
        settings = self.storage.get_settings()
        settings.subscription_chat_id = chat_id
        settings.subscription_invite_link = invite_link
        self.storage.save_settings(settings)
        self.apply_settings(settings)
        return settings
