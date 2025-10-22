"""Storage layer abstractions."""

from __future__ import annotations

from copy import deepcopy
from typing import Dict, Iterable, Optional

from .models import BotSettings, Invoice, Post, User


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
