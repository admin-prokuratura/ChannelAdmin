"""Storage layer abstractions."""

from __future__ import annotations

from copy import deepcopy
from typing import Dict, Iterable, List, Optional

from .models import Post, User


class AbstractStorage:
    """Interface for persisting users and posts."""

    def get_user(self, user_id: int) -> Optional[User]:
        raise NotImplementedError

    def save_user(self, user: User) -> None:
        raise NotImplementedError

    def add_post(self, post: Post) -> None:
        raise NotImplementedError

    def list_posts(self) -> Iterable[Post]:
        raise NotImplementedError


class InMemoryStorage(AbstractStorage):
    """Simple dictionary-based storage for demos and tests."""

    def __init__(self) -> None:
        self._users: Dict[int, User] = {}
        self._posts: List[Post] = []

    def get_user(self, user_id: int) -> Optional[User]:
        user = self._users.get(user_id)
        if user is None:
            return None
        return deepcopy(user)

    def save_user(self, user: User) -> None:
        self._users[user.user_id] = deepcopy(user)

    def add_post(self, post: Post) -> None:
        self._posts.append(post)

    def list_posts(self) -> Iterable[Post]:
        return list(self._posts)
