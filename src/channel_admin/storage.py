"""Storage layer abstractions."""

from __future__ import annotations

import contextlib
import json
import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional

from .models import BotSettings, GoldenCard, Invoice, Post, User, utcnow

LOGGER = logging.getLogger(__name__)


def _datetime_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _iso_to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        LOGGER.warning("Failed to parse datetime value %r", value)
        return None


def _timedelta_to_seconds(value: timedelta | None) -> float | None:
    if value is None:
        return None
    return float(value.total_seconds())


def _seconds_to_timedelta(value: float | int | None) -> timedelta | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        LOGGER.warning("Failed to parse timedelta value %r", value)
        return None
    return timedelta(seconds=seconds)


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        LOGGER.warning("Failed to parse integer value %r; using %s", value, default)
        return default


def _safe_optional_int(value: object | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        LOGGER.warning("Failed to parse optional integer value %r", value)
        return None


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        LOGGER.warning("Failed to parse float value %r; using %s", value, default)
        return default


def _serialize_golden_card(card: GoldenCard) -> dict:
    return {
        "duration_seconds": _timedelta_to_seconds(card.duration),
        "purchased_at": _datetime_to_iso(card.purchased_at),
    }


def _deserialize_golden_card(payload: dict) -> GoldenCard | None:
    duration = _seconds_to_timedelta(payload.get("duration_seconds"))
    if duration is None:
        return None
    purchased_at = _iso_to_datetime(payload.get("purchased_at"))
    if purchased_at is None:
        purchased_at = utcnow()
    return GoldenCard(duration=duration, purchased_at=purchased_at)


def _serialize_user(user: User) -> dict:
    return {
        "user_id": user.user_id,
        "energy": user.energy,
        "golden_cards": [
            payload
            for card in user.golden_cards
            if (payload := _serialize_golden_card(card))
        ],
        "referred_users": sorted(user.referred_users),
        "is_banned": user.is_banned,
        "is_admin": user.is_admin,
        "username": user.username,
        "full_name": user.full_name,
    }


def _deserialize_user(payload: dict) -> User:
    golden_cards: list[GoldenCard] = []
    for raw_card in payload.get("golden_cards", []):
        card = _deserialize_golden_card(raw_card)
        if card is not None:
            golden_cards.append(card)

    referred_users: set[int] = set()
    for raw_user_id in payload.get("referred_users", []):
        try:
            referred_users.add(int(raw_user_id))
        except (TypeError, ValueError):
            LOGGER.warning("Skipping invalid referred user id %r", raw_user_id)

    return User(
        user_id=_safe_int(payload.get("user_id", 0)),
        energy=_safe_int(payload.get("energy", 0)),
        golden_cards=golden_cards,
        referred_users=referred_users,
        is_banned=bool(payload.get("is_banned", False)),
        is_admin=bool(payload.get("is_admin", False)),
        username=payload.get("username"),
        full_name=payload.get("full_name"),
    )


def _serialize_post(post: Post) -> dict:
    return {
        "post_id": post.post_id,
        "user_id": post.user_id,
        "text": post.text,
        "requires_pin": post.requires_pin,
        "created_at": _datetime_to_iso(post.created_at),
        "status": post.status,
        "channel_message_id": post.channel_message_id,
        "chat_message_id": post.chat_message_id,
        "button_text": post.button_text,
        "button_url": post.button_url,
        "photo_file_id": post.photo_file_id,
        "parse_mode": post.parse_mode,
    }


def _deserialize_post(payload: dict) -> Post:
    return Post(
        post_id=_safe_optional_int(payload.get("post_id")),
        user_id=_safe_int(payload.get("user_id", 0)),
        text=str(payload.get("text") or ""),
        requires_pin=bool(payload.get("requires_pin", False)),
        created_at=_iso_to_datetime(payload.get("created_at"))
        or datetime.fromtimestamp(0, tz=timezone.utc),
        status=str(payload.get("status") or "pending"),
        channel_message_id=_safe_optional_int(payload.get("channel_message_id")),
        chat_message_id=_safe_optional_int(payload.get("chat_message_id")),
        button_text=payload.get("button_text"),
        button_url=payload.get("button_url"),
        photo_file_id=payload.get("photo_file_id"),
        parse_mode=payload.get("parse_mode"),
    )


def _serialize_invoice(invoice: Invoice) -> dict:
    return {
        "invoice_id": invoice.invoice_id,
        "user_id": invoice.user_id,
        "invoice_type": invoice.invoice_type,
        "amount": invoice.amount,
        "asset": invoice.asset,
        "pay_url": invoice.pay_url,
        "price": invoice.price,
        "status": invoice.status,
        "created_at": _datetime_to_iso(invoice.created_at),
        "paid_at": _datetime_to_iso(invoice.paid_at),
        "payload": invoice.payload,
        "energy_amount": invoice.energy_amount,
        "golden_hours": invoice.golden_hours,
    }


def _deserialize_invoice(payload: dict) -> Invoice:
    return Invoice(
        invoice_id=_safe_int(payload.get("invoice_id", 0)),
        user_id=_safe_int(payload.get("user_id", 0)),
        invoice_type=str(payload.get("invoice_type") or ""),
        amount=_safe_float(payload.get("amount", 0.0)),
        asset=str(payload.get("asset") or ""),
        pay_url=str(payload.get("pay_url") or ""),
        price=_safe_float(payload.get("price", 0.0)),
        status=str(payload.get("status") or "pending"),
        created_at=_iso_to_datetime(payload.get("created_at"))
        or datetime.fromtimestamp(0, tz=timezone.utc),
        paid_at=_iso_to_datetime(payload.get("paid_at")),
        payload=payload.get("payload"),
        energy_amount=_safe_optional_int(payload.get("energy_amount")),
        golden_hours=_safe_optional_int(payload.get("golden_hours")),
    )


def _serialize_settings(settings: BotSettings) -> dict:
    return {
        "autopost_paused": settings.autopost_paused,
        "post_energy_cost": settings.post_energy_cost,
        "energy_price_per_unit": settings.energy_price_per_unit,
    }


def _deserialize_settings(payload: dict | None) -> BotSettings:
    if not payload:
        return BotSettings()
    return BotSettings(
        autopost_paused=bool(payload.get("autopost_paused", False)),
        post_energy_cost=_safe_int(
            payload.get("post_energy_cost", BotSettings.post_energy_cost),
            BotSettings.post_energy_cost,
        ),
        energy_price_per_unit=_safe_float(
            payload.get("energy_price_per_unit", BotSettings.energy_price_per_unit),
            BotSettings.energy_price_per_unit,
        ),
    )


class AbstractStorage:
    """Interface for persisting users and posts."""

    def get_user(self, user_id: int) -> Optional[User]:
        raise NotImplementedError

    def save_user(self, user: User) -> None:
        raise NotImplementedError

    def list_users(self) -> Iterable[User]:
        raise NotImplementedError

    def add_post(self, post: Post) -> None:
        raise NotImplementedError

    def list_posts(self) -> Iterable[Post]:
        raise NotImplementedError

    def get_post(self, post_id: int) -> Optional[Post]:
        raise NotImplementedError

    def save_post(self, post: Post) -> None:
        raise NotImplementedError

    def list_posts_by_status(self, status: str) -> Iterable[Post]:
        raise NotImplementedError

    def list_posts_for_user(
        self, user_id: int, statuses: Optional[set[str]] | None = None
    ) -> Iterable[Post]:
        raise NotImplementedError

    def save_invoice(self, invoice: Invoice) -> None:
        raise NotImplementedError

    def get_invoice(self, invoice_id: int) -> Optional[Invoice]:
        raise NotImplementedError

    def list_invoices(self) -> Iterable[Invoice]:
        raise NotImplementedError

    def list_invoices_for_user(self, user_id: int) -> Iterable[Invoice]:
        raise NotImplementedError

    def save_settings(self, settings: BotSettings) -> None:
        raise NotImplementedError

    def get_settings(self) -> BotSettings:
        raise NotImplementedError

    def count_users(self) -> int:
        raise NotImplementedError

    def count_posts(self, status: Optional[str] = None) -> int:
        raise NotImplementedError


class InMemoryStorage(AbstractStorage):
    """Simple dictionary-based storage for demos and tests."""

    def __init__(self) -> None:
        self._users: Dict[int, User] = {}
        self._posts: Dict[int, Post] = {}
        self._post_sequence: int = 1
        self._invoices: Dict[int, Invoice] = {}
        self._settings: BotSettings = BotSettings()

    def get_user(self, user_id: int) -> Optional[User]:
        user = self._users.get(user_id)
        if user is None:
            return None
        return deepcopy(user)

    def save_user(self, user: User) -> None:
        self._users[user.user_id] = deepcopy(user)

    def list_users(self) -> Iterable[User]:
        return [deepcopy(user) for user in self._users.values()]

    def add_post(self, post: Post) -> None:
        if post.post_id is None:
            post.post_id = self._post_sequence
            self._post_sequence += 1
        self._posts[post.post_id] = deepcopy(post)

    def list_posts(self) -> Iterable[Post]:
        return [deepcopy(post) for post in sorted(self._posts.values(), key=lambda p: p.created_at)]

    def get_post(self, post_id: int) -> Optional[Post]:
        post = self._posts.get(post_id)
        if post is None:
            return None
        return deepcopy(post)

    def save_post(self, post: Post) -> None:
        if post.post_id is None:
            raise ValueError("Post must have an id before saving")
        self._posts[post.post_id] = deepcopy(post)

    def list_posts_by_status(self, status: str) -> Iterable[Post]:
        return [
            deepcopy(post)
            for post in sorted(self._posts.values(), key=lambda p: p.created_at)
            if post.status == status
        ]

    def list_posts_for_user(
        self, user_id: int, statuses: Optional[set[str]] | None = None
    ) -> Iterable[Post]:
        return [
            deepcopy(post)
            for post in sorted(self._posts.values(), key=lambda p: p.created_at)
            if post.user_id == user_id and (statuses is None or post.status in statuses)
        ]

    def save_invoice(self, invoice: Invoice) -> None:
        self._invoices[invoice.invoice_id] = deepcopy(invoice)

    def get_invoice(self, invoice_id: int) -> Optional[Invoice]:
        invoice = self._invoices.get(invoice_id)
        if invoice is None:
            return None
        return deepcopy(invoice)

    def list_invoices_for_user(self, user_id: int) -> Iterable[Invoice]:
        return [
            deepcopy(invoice)
            for invoice in self._invoices.values()
            if invoice.user_id == user_id
        ]

    def list_invoices(self) -> Iterable[Invoice]:
        return [deepcopy(invoice) for invoice in self._invoices.values()]

    def save_settings(self, settings: BotSettings) -> None:
        self._settings = deepcopy(settings)

    def get_settings(self) -> BotSettings:
        return deepcopy(self._settings)

    def count_users(self) -> int:
        return len(self._users)

    def count_posts(self, status: Optional[str] = None) -> int:
        if status is None:
            return len(self._posts)
        return sum(1 for post in self._posts.values() if post.status == status)


class JsonStorage(InMemoryStorage):
    """JSON-backed storage persisted on disk."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__()
        self._load()

    # Persistence helpers -------------------------------------------------

    def _persist(self) -> None:
        payload = {
            "users": {str(user_id): _serialize_user(user) for user_id, user in self._users.items()},
            "posts": {str(post_id): _serialize_post(post) for post_id, post in self._posts.items()},
            "post_sequence": self._post_sequence,
            "invoices": {
                str(invoice_id): _serialize_invoice(invoice)
                for invoice_id, invoice in self._invoices.items()
            },
            "settings": _serialize_settings(self._settings),
        }
        temp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            temp_path.replace(self._path)
        except OSError as exc:
            LOGGER.error("Failed to write storage file %s: %s", self._path, exc)
            with contextlib.suppress(OSError):
                temp_path.unlink(missing_ok=True)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except json.JSONDecodeError as exc:
            LOGGER.error("Failed to parse storage file %s: %s", self._path, exc)
            return
        except OSError as exc:
            LOGGER.error("Failed to read storage file %s: %s", self._path, exc)
            return

        try:
            raw_users = payload.get("users", {}) or {}
            self._users = {
                int(user_id): _deserialize_user({"user_id": user_id, **user_payload})
                for user_id, user_payload in raw_users.items()
            }

            raw_posts = payload.get("posts", {}) or {}
            self._posts = {
                int(post_id): _deserialize_post({"post_id": int(post_id), **post_payload})
                for post_id, post_payload in raw_posts.items()
            }

            raw_invoices = payload.get("invoices", {}) or {}
            self._invoices = {
                int(invoice_id): _deserialize_invoice(
                    {"invoice_id": invoice_id, **invoice_payload}
                )
                for invoice_id, invoice_payload in raw_invoices.items()
            }

            stored_sequence = payload.get("post_sequence")
            if isinstance(stored_sequence, int) and stored_sequence > 0:
                self._post_sequence = stored_sequence
            else:
                self._post_sequence = (
                    max(self._posts.keys(), default=0) + 1
                    if self._posts
                    else 1
                )

            self._settings = _deserialize_settings(payload.get("settings"))
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.error("Failed to load storage data from %s: %s", self._path, exc)
            # Revert to clean in-memory state on error
            super().__init__()

    # AbstractStorage implementation -------------------------------------

    def save_user(self, user: User) -> None:
        super().save_user(user)
        self._persist()

    def add_post(self, post: Post) -> None:
        super().add_post(post)
        self._persist()

    def save_post(self, post: Post) -> None:
        super().save_post(post)
        self._persist()

    def save_invoice(self, invoice: Invoice) -> None:
        super().save_invoice(invoice)
        self._persist()

    def save_settings(self, settings: BotSettings) -> None:
        super().save_settings(settings)
        self._persist()
