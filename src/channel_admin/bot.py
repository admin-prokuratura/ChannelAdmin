"""Executable Telegram bot wiring for the Channel Admin service."""

from __future__ import annotations
import html
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    AIORateLimiter,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from dotenv import load_dotenv

from .config import FilterConfig, PricingConfig
from .services import ChannelEconomyService
from .storage import AbstractStorage, InMemoryStorage, JsonStorage
from .payments import CryptoPayClient, CryptoPayError
from .models import BotSettings, Invoice, Ticket, utcnow

LOGGER = logging.getLogger(__name__)


load_dotenv()


def _parse_int_env(name: str) -> int | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        LOGGER.warning("Environment variable %s must be an integer (got %r)", name, raw)
        return None


def _parse_float_env(name: str) -> float | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        normalized = raw.replace(",", ".")
        return float(normalized)
    except ValueError:
        LOGGER.warning("Environment variable %s must be a number (got %r)", name, raw)
        return None


def _parse_admin_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    admin_ids: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            admin_ids.add(int(chunk))
        except ValueError:
            LOGGER.warning("Skipping invalid admin id %r", chunk)
    return admin_ids


ADMIN_USER_IDS: set[int] = _parse_admin_ids(os.environ.get("ADMIN_USER_IDS"))
TELEGRAM_CHANNEL_ID: int | None = _parse_int_env("TELEGRAM_CHANNEL_ID")
TELEGRAM_CHAT_ID: int | None = _parse_int_env("TELEGRAM_CHAT_ID")
AUTOPOST_INTERVAL_SECONDS: int = max(int(os.environ.get("AUTOPOST_INTERVAL_SECONDS", "60")), 10)
PAID_INVOICE_STATUSES: set[str] = {"paid", "completed"}
JSON_STORAGE_PATH: str | None = os.environ.get("JSON_STORAGE_PATH")
DEFAULT_JSON_STORAGE_PATH = Path.home() / ".channel_admin" / "storage.json"
GOLDEN_CARD_PRESETS: tuple[int, ...] = (12, 24, 72)


def _format_rubles(amount: float | None) -> str:
    if amount is None:
        return ""
    formatted = f"{amount:.2f}".rstrip("0").rstrip(".")
    return formatted or "0"


def _subscription_link(settings: BotSettings) -> str | None:
    if settings.subscription_invite_link:
        return settings.subscription_invite_link
    chat_id = settings.subscription_chat_id
    if not chat_id:
        return None
    if isinstance(chat_id, str) and chat_id.startswith("@"):
        slug = chat_id[1:]
        return f"https://t.me/{slug}" if slug else None
    if isinstance(chat_id, str) and not chat_id.startswith("-"):
        return f"https://t.me/{chat_id}"
    return None


def _parse_subscription_input(raw: str) -> tuple[str, str | None]:
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("Отправьте @username, числовой ID или ссылку на канал.")

    parts = cleaned.split()
    chat_candidate: str | None = None
    link_candidate: str | None = None

    for part in parts:
        if part.startswith(("http://", "https://")):
            link_candidate = part
        elif chat_candidate is None:
            chat_candidate = part

    chat_id: str | None = None
    invite_link: str | None = link_candidate

    if invite_link:
        parsed = urlparse(invite_link)
        slug = parsed.path.strip("/")
        if parsed.netloc in {"t.me", "telegram.me"} and slug and not slug.startswith("+"):
            chat_id = f"@{slug}"

    if chat_candidate:
        candidate = chat_candidate
        if candidate.startswith("@"):
            chat_id = candidate
        elif candidate.lstrip("-").isdigit():
            chat_id = candidate
        else:
            chat_id = f"@{candidate}"

    if chat_id is None:
        raise ValueError(
            "Не удалось определить идентификатор канала. "
            "Укажите @username, числовой ID или добавьте ссылку формата https://t.me/..."
        )

    if invite_link is None and chat_id.startswith("@"):
        invite_link = f"https://t.me/{chat_id[1:]}" if len(chat_id) > 1 else None

    return chat_id, invite_link


async def send_subscription_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: BotSettings,
    *,
    error: str | None = None,
) -> None:
    link = _subscription_link(settings)
    channel_label = settings.subscription_invite_link or settings.subscription_chat_id or "каналу"
    lines = [
        "🔒 <b>Доступ только для подписчиков</b>",
        "Подпишитесь на канал, чтобы пользоваться ботом.",
        f"Сейчас доступ открыт только подписчикам: <b>{html.escape(str(channel_label))}</b>.",
        "После подписки вернитесь в бот и нажмите «🔄 Проверить подписку».",
    ]
    if error:
        lines.append("")
        lines.append(f"⚠️ {html.escape(error)}")

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    if link:
        keyboard_rows.append([InlineKeyboardButton("📢 Открыть канал", url=link)])
    keyboard_rows.append(
        [InlineKeyboardButton("🔄 Проверить подписку", callback_data="action:check_subscription")]
    )

    text = "\n\n".join(lines)
    markup = InlineKeyboardMarkup(keyboard_rows)

    if update.callback_query:
        await update.callback_query.message.edit_text(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    elif update.message:
        await update.message.reply_text(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def ensure_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    show_prompt: bool = True,
) -> bool:
    ensure_dependencies(context)
    service = get_service(context)
    settings = service.current_settings or service.get_settings()
    requirement = settings.subscription_chat_id
    if not requirement:
        return True

    user = update.effective_user
    if user is None:
        return False

    target_chat: str | int = requirement
    if isinstance(requirement, str) and requirement.lstrip("-").isdigit():
        try:
            target_chat = int(requirement)
        except ValueError:
            target_chat = requirement

    try:
        member = await context.bot.get_chat_member(target_chat, user.id)
    except TelegramError as exc:
        LOGGER.warning("Failed to verify subscription for %s: %s", user.id, exc)
        if show_prompt:
            await send_subscription_prompt(
                update,
                context,
                settings,
                error="Не удалось проверить подписку. Попробуйте снова позже.",
            )
        return False

    status = getattr(member, "status", None)
    is_member = getattr(member, "is_member", None)
    subscribed = status not in {"left", "kicked"}
    if is_member is not None:
        subscribed = subscribed and bool(is_member)

    if not subscribed:
        if show_prompt:
            await send_subscription_prompt(update, context, settings)
        return False

    return True


def build_service() -> ChannelEconomyService:
    pricing = PricingConfig()
    rate = _parse_float_env("RUB_PER_USD")
    if rate is not None:
        if rate <= 0:
            LOGGER.warning("RUB_PER_USD must be positive, got %s", rate)
        else:
            pricing.rubles_per_usd = rate
    filter_config = FilterConfig()
    storage = build_storage()
    return ChannelEconomyService(storage=storage, pricing=pricing, filter_config=filter_config)


def build_storage() -> AbstractStorage:
    storage_path = Path(JSON_STORAGE_PATH) if JSON_STORAGE_PATH else DEFAULT_JSON_STORAGE_PATH
    try:
        return JsonStorage(storage_path)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.error(
            "Failed to initialise JSON storage at %s: %s", storage_path, exc
        )
    return InMemoryStorage()


def build_crypto_client() -> CryptoPayClient | None:
    token = os.environ.get("CRYPTOPAY_TOKEN")
    if not token:
        LOGGER.warning("CRYPTOPAY_TOKEN is not configured; payments will be disabled")
        return None
    return CryptoPayClient(token=token)


def _clean_full_name(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def sync_user_profile(update: Update, service: ChannelEconomyService) -> None:
    tg_user = update.effective_user
    if tg_user is None:
        return
    if service.get_user_balance(tg_user.id) is None:
        return
    full_name = _clean_full_name(tg_user.full_name)
    service.update_user_profile(
        tg_user.id,
        username=tg_user.username,
        full_name=full_name,
    )


def ensure_dependencies(context: ContextTypes.DEFAULT_TYPE) -> None:
    service = context.application.bot_data.setdefault("service", build_service())
    service.apply_settings(service.get_settings())
    context.application.bot_data.setdefault("crypto", build_crypto_client())


def is_admin_id(user_id: int | None, context: ContextTypes.DEFAULT_TYPE | None = None) -> bool:
    if user_id is None:
        return False
    if user_id in ADMIN_USER_IDS:
        return True
    if context is None:
        return False
    service = context.application.bot_data.get("service") if hasattr(context, "application") else None
    if service:
        user = service.get_user_balance(user_id)
        if user and user.is_admin:
            return True
    return False


def main_menu_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📊 Баланс", callback_data="action:balance"),
            InlineKeyboardButton("⚡️ Пополнить энергию", callback_data="action:energy"),
        ],
        [
            InlineKeyboardButton("🌟 Золотая карточка", callback_data="action:golden_card"),
            InlineKeyboardButton("🆘 Поддержка", callback_data="action:support"),
        ],
        [
            InlineKeyboardButton("📝 Отправить пост", callback_data="action:post"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🎛 Админ-панель", callback_data="action:admin")])
    return InlineKeyboardMarkup(rows)


def energy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("50 ⚡️", callback_data="energy:50"),
                InlineKeyboardButton("100 ⚡️", callback_data="energy:100"),
            ],
            [
                InlineKeyboardButton("250 ⚡️", callback_data="energy:250"),
                InlineKeyboardButton("Другая сумма", callback_data="energy:custom"),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="action:menu")],
        ]
    )


def golden_card_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("12 ч", callback_data="golden:12"),
                InlineKeyboardButton("24 ч", callback_data="golden:24"),
            ],
            [InlineKeyboardButton("72 ч", callback_data="golden:72")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="action:menu")],
        ]
    )


ADMIN_USERS_PAGE_SIZE = 5
POST_PREVIEW_LENGTH = 400
SUPPORT_HISTORY_LIMIT = 10
SUPPORT_TICKETS_PAGE_SIZE = 5


def _format_ticket_subject(ticket: Ticket) -> str:
    subject = ticket.subject
    if not subject and ticket.messages:
        subject = ticket.messages[0].text
    if not subject:
        subject = "Без темы"
    normalized = " ".join(subject.split())
    if len(normalized) > 60:
        normalized = normalized[:57] + "..."
    return normalized or "Без темы"


def _format_ticket_timestamp(when: datetime) -> str:
    try:
        local_time = when.astimezone()
    except ValueError:  # pragma: no cover - timezone edge case
        local_time = when
    return local_time.strftime("%d.%m %H:%M")


def _format_ticket_messages(
    ticket: Ticket, *, viewer: str, limit: int = SUPPORT_HISTORY_LIMIT
) -> str:
    messages = ticket.messages[-limit:]
    if not messages:
        return "Сообщений пока нет."
    lines: list[str] = []
    for message in messages:
        timestamp = _format_ticket_timestamp(message.created_at)
        if message.sender == "user":
            sender_label = "👤 Вы" if viewer == "user" else "👤 Пользователь"
        else:
            sender_label = "🛠 Админ" if viewer == "user" else "🛠 Вы"
        lines.append(f"[{timestamp}] {sender_label}:")
        lines.append(message.text)
        lines.append("")
    return "\n".join(lines).rstrip()


async def show_support_overview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str | None = None,
) -> None:
    service = get_service(context)
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None:
        if update.callback_query:
            await update.callback_query.answer("Не удалось определить пользователя", show_alert=True)
        elif update.message:
            await update.message.reply_text("Не удалось определить пользователя.")
        return

    user = service.get_user_balance(user_id)
    if user is None:
        if update.callback_query:
            await update.callback_query.answer("Сначала используйте /start", show_alert=True)
            await update.callback_query.message.edit_text(
                "❗️ Пользователь не найден. Нажмите /start для регистрации.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Назад", callback_data="action:menu")]]
                ),
            )
        elif update.message:
            await update.message.reply_text("Пожалуйста, зарегистрируйтесь командой /start.")
        return

    tickets = service.list_user_tickets(user_id)
    lines = ["🆘 Поддержка"]
    if notice:
        lines.append("")
        lines.append(notice)

    if tickets:
        lines.append("")
        lines.append("Ваши обращения:")
        for ticket in tickets:
            status_icon = "🟢" if ticket.status == "open" else "⚪️"
            lines.append(f"{status_icon} #{ticket.ticket_id} — {_format_ticket_subject(ticket)}")
    else:
        lines.append("")
        lines.append(
            "У вас пока нет обращений. Нажмите «📨 Новый тикет», чтобы описать вопрос."
        )

    keyboard_rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("📨 Новый тикет", callback_data="support:new")]
    ]
    for ticket in tickets[:10]:
        icon = "🔓" if ticket.status == "open" else "🔒"
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    f"{icon} #{ticket.ticket_id}",
                    callback_data=f"support:view:{ticket.ticket_id}",
                )
            ]
        )
    keyboard_rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="action:menu")])

    keyboard = InlineKeyboardMarkup(keyboard_rows)
    text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=keyboard)
    elif update.message:
        await update.message.reply_text(text, reply_markup=keyboard)


async def show_support_ticket_detail(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ticket_id: int,
    *,
    info: str | None = None,
) -> None:
    service = get_service(context)
    user_id = update.effective_user.id if update.effective_user else None
    ticket = service.get_ticket(ticket_id)
    if ticket is None or (user_id is not None and ticket.user_id != user_id and not is_admin_id(user_id, context)):
        if update.callback_query:
            await update.callback_query.answer("Тикет не найден", show_alert=True)
        elif update.message:
            await update.message.reply_text("Тикет не найден или доступ ограничен.")
        return

    status_text = "🟢 Открыт" if ticket.status == "open" else "✅ Закрыт"
    lines = [
        f"🗂 Тикет #{ticket.ticket_id}",
        status_text,
        f"Тема: {_format_ticket_subject(ticket)}",
        "",
    ]
    if info:
        lines.append(info)
        lines.append("")
    lines.append(_format_ticket_messages(ticket, viewer="user"))

    buttons: list[list[InlineKeyboardButton]] = []
    if ticket.status == "open":
        buttons.append(
            [
                InlineKeyboardButton(
                    "✍️ Ответить", callback_data=f"support:reply:{ticket.ticket_id}"
                )
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    "✅ Закрыть", callback_data=f"support:close:{ticket.ticket_id}"
                )
            ]
        )
    else:
        buttons.append(
            [
                InlineKeyboardButton(
                    "♻️ Открыть снова", callback_data=f"support:reopen:{ticket.ticket_id}"
                )
            ]
        )
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="support:list")])
    keyboard = InlineKeyboardMarkup(buttons)

    text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=keyboard)
    elif update.message:
        await update.message.reply_text(text, reply_markup=keyboard)


async def show_admin_support_overview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    info: str | None = None,
) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin_id(user_id, context):
        if update.callback_query:
            await update.callback_query.answer("Доступ запрещён", show_alert=True)
        elif update.message:
            await update.message.reply_text("Доступ запрещён.")
        return

    filter_state = context.user_data.get("admin_support_filter", "open")
    page = context.user_data.get("admin_support_page", 0)
    service = get_service(context)
    status_filter = None if filter_state == "all" else "open"
    tickets = service.list_tickets(status=status_filter)

    total = len(tickets)
    if total == 0:
        text = "🆘 Обращений не найдено."
        if info:
            text += f"\n\n{info}"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Назад", callback_data="admin:refresh")]]
        )
        if update.callback_query:
            await update.callback_query.message.edit_text(text, reply_markup=keyboard)
        elif update.message:
            await update.message.reply_text(text, reply_markup=keyboard)
        return

    tickets.sort(key=lambda t: t.updated_at, reverse=True)
    max_page = (total - 1) // SUPPORT_TICKETS_PAGE_SIZE
    page = max(0, min(page, max_page))
    context.user_data["admin_support_page"] = page

    start = page * SUPPORT_TICKETS_PAGE_SIZE
    subset = tickets[start : start + SUPPORT_TICKETS_PAGE_SIZE]

    lines = ["🆘 Поддержка"]
    if info:
        lines.append("")
        lines.append(info)
    lines.append("")
    lines.append(
        "Показываются "
        + ("только открытые" if filter_state == "open" else "все")
        + f" тикеты (страница {page + 1} из {max_page + 1})."
    )
    lines.append("")
    for ticket in subset:
        status_icon = "🟢" if ticket.status == "open" else "⚪️"
        lines.append(
            f"{status_icon} #{ticket.ticket_id} • {ticket.user_id} — {_format_ticket_subject(ticket)}"
        )

    buttons: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                "🟢 Открытые" + (" ✅" if filter_state == "open" else ""),
                callback_data="admin:support:filter:open",
            ),
            InlineKeyboardButton(
                "📁 Все" + (" ✅" if filter_state == "all" else ""),
                callback_data="admin:support:filter:all",
            ),
        ]
    ]

    for ticket in subset:
        buttons.append(
            [
                InlineKeyboardButton(
                    f"#{ticket.ticket_id} ({'🔓' if ticket.status == 'open' else '🔒'})",
                    callback_data=f"admin:support:view:{ticket.ticket_id}",
                )
            ]
        )

    if max_page > 0:
        prev_page = (page - 1) % (max_page + 1)
        next_page = (page + 1) % (max_page + 1)
        buttons.append(
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data=f"admin:support:page:{prev_page}",
                ),
                InlineKeyboardButton(
                    "Вперёд ➡️",
                    callback_data=f"admin:support:page:{next_page}",
                ),
            ]
        )

    buttons.append([InlineKeyboardButton("🎛 Админ-панель", callback_data="admin:refresh")])

    text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup(buttons)
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=keyboard)
    elif update.message:
        await update.message.reply_text(text, reply_markup=keyboard)


async def show_admin_ticket_detail(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ticket_id: int,
    *,
    info: str | None = None,
) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin_id(user_id, context):
        if update.callback_query:
            await update.callback_query.answer("Доступ запрещён", show_alert=True)
        elif update.message:
            await update.message.reply_text("Доступ запрещён.")
        return

    service = get_service(context)
    ticket = service.get_ticket(ticket_id)
    if ticket is None:
        if update.callback_query:
            await update.callback_query.answer("Тикет не найден", show_alert=True)
        elif update.message:
            await update.message.reply_text("Тикет не найден.")
        return

    status_text = "🟢 Открыт" if ticket.status == "open" else "✅ Закрыт"
    lines = [
        f"🗂 Тикет #{ticket.ticket_id}",
        status_text,
        f"Пользователь: {ticket.user_id}",
        f"Тема: {_format_ticket_subject(ticket)}",
        "",
    ]
    if info:
        lines.append(info)
        lines.append("")
    lines.append(_format_ticket_messages(ticket, viewer="admin"))

    buttons: list[list[InlineKeyboardButton]] = []
    if ticket.status == "open":
        buttons.append(
            [
                InlineKeyboardButton(
                    "✍️ Ответить", callback_data=f"admin:support:reply:{ticket.ticket_id}"
                )
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    "✅ Закрыть", callback_data=f"admin:support:close:{ticket.ticket_id}"
                )
            ]
        )
    else:
        buttons.append(
            [
                InlineKeyboardButton(
                    "♻️ Открыть снова", callback_data=f"admin:support:reopen:{ticket.ticket_id}"
                )
            ]
        )
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:support")])

    keyboard = InlineKeyboardMarkup(buttons)
    text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=keyboard)
    elif update.message:
        await update.message.reply_text(text, reply_markup=keyboard)


async def send_main_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    ensure_dependencies(context)
    service = get_service(context)
    tg_user = update.effective_user
    user = service.get_user_balance(tg_user.id) if tg_user else None
    user_id = tg_user.id if tg_user else None
    keyboard = main_menu_keyboard(is_admin=is_admin_id(user_id, context))

    energy = user.energy if user else 0
    active_cards = (
        sum(1 for card in user.golden_cards if card.expires_at > utcnow())
        if user
        else 0
    )
    total_cards = len(user.golden_cards) if user else 0
    display_name = (
        user.full_name
        or (tg_user.full_name if tg_user else None)
        or (tg_user.username if tg_user else None)
        or "Пользователь"
    )

    post_cost = service.post_energy_cost
    rub_note = None
    if service.current_settings and service.current_settings.energy_price_per_unit > 0:
        rub_note = service.current_settings.energy_price_per_unit * post_cost

    cost_line = f"{post_cost} ⚡️"
    if rub_note:
        cost_line += f" (~{_format_rubles(rub_note)} ₽)"

    lines = [
        "✨ <b>Главное меню</b>",
        f"👤 <b>{html.escape(display_name)}</b>",
        "",
        f"⚡️ Энергия: <b>{energy}</b>",
        f"🌟 Золотые карточки: <b>{active_cards}</b> из {total_cards}",
        f"🧾 Стоимость поста: <b>{cost_line}</b>",
    ]

    if text:
        lines.append("")
        lines.append(html.escape(text, quote=False))

    lines.append("")
    lines.append("Выберите нужный раздел на клавиатуре ниже.")

    menu_text = "\n".join(lines)

    if update.message:
        await update.message.reply_text(
            menu_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    elif update.callback_query:
        await update.callback_query.message.edit_text(
            menu_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


def get_service(context: ContextTypes.DEFAULT_TYPE) -> ChannelEconomyService:
    return context.application.bot_data.setdefault("service", build_service())


def get_crypto_client(context: ContextTypes.DEFAULT_TYPE) -> CryptoPayClient | None:
    client = context.application.bot_data.get("crypto")
    if client is None:
        client = build_crypto_client()
        if client:
            context.application.bot_data["crypto"] = client
    return client


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service = get_service(context)
    tg_user = update.effective_user
    if tg_user is None:
        if update.message:
            await update.message.reply_text("Не удалось определить пользователя.")
        return
    if not await ensure_subscription(update, context):
        return
    full_name = _clean_full_name(tg_user.full_name)
    try:
        service.register_user(
            tg_user.id,
            subscribed_to_sponsors=True,
            username=tg_user.username,
            full_name=full_name,
        )
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    sync_user_profile(update, service)
    ensure_dependencies(context)
    user = service.get_user_balance(tg_user.id)
    if user and user.is_banned and not is_admin_id(tg_user.id, context):
        await update.message.reply_text("Ваш доступ к боту ограничен.")
        return
    await update.message.reply_text(
        "👋 Добро пожаловать! Давайте начнём работу с ботом.",
        disable_web_page_preview=True,
    )
    await send_main_menu(update, context, "Выберите раздел ниже")


async def handle_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    query = update.callback_query
    assert query is not None
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "check_subscription":
        if await ensure_subscription(update, context, show_prompt=False):
            await send_main_menu(
                update,
                context,
                "✅ Подписка подтверждена! Выберите действие.",
            )
        else:
            service = get_service(context)
            settings = service.current_settings or service.get_settings()
            await send_subscription_prompt(update, context, settings)
        return

    if action == "menu":
        context.user_data.pop("post_creation", None)
        context.user_data.pop("awaiting_custom_energy", None)
        context.user_data.pop("awaiting_post_price", None)
        context.user_data.pop("awaiting_energy_price", None)
        context.user_data.pop("awaiting_user_balance", None)
        context.user_data.pop("support_new_ticket", None)
        context.user_data.pop("support_reply", None)
        context.user_data.pop("admin_ticket_reply", None)
        context.user_data.pop("awaiting_subscription_target", None)
        await send_main_menu(update, context, "Выберите действие из меню 👇")
        return

    if not await ensure_subscription(update, context):
        return

    service = get_service(context)
    sync_user_profile(update, service)
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None:
        await query.answer("Не удалось определить пользователя", show_alert=True)
        return

    user = service.get_user_balance(user_id)
    if user is None and action != "admin":
        await query.message.edit_text(
            "❗️ Пользователь не найден. Нажмите /start для регистрации.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 В меню", callback_data="action:menu")]]
            ),
        )
        return

    if user and user.is_banned and action != "admin" and not is_admin_id(user_id, context):
        await query.message.edit_text(
            "🚫 Ваш доступ к функциям бота ограничен.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 В меню", callback_data="action:menu")]]
            ),
        )
        return

    if action == "balance":
        if not user:
            await query.message.edit_text(
                "❗️ Пользователь не найден. Нажмите /start для регистрации.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 В меню", callback_data="action:menu")]]
                ),
            )
            return
        active_cards = sum(1 for card in user.golden_cards if card.expires_at > utcnow())
        total_cards = len(user.golden_cards)
        post_cost = service.post_energy_cost
        rub_note = None
        if service.current_settings and service.current_settings.energy_price_per_unit > 0:
            rub_note = service.current_settings.energy_price_per_unit * post_cost
        cost_line = f"{post_cost} ⚡️"
        if rub_note:
            cost_line += f" (~{_format_rubles(rub_note)} ₽)"
        await query.message.edit_text(
            "\n".join(
                [
                    "📊 <b>Ваш профиль</b>",
                    f"⚡️ Энергия: <b>{user.energy}</b>",
                    f"🌟 Активные карточки: <b>{active_cards}</b> из {total_cards}",
                    f"🧾 Стоимость поста: <b>{cost_line}</b>",
                ]
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 В меню", callback_data="action:menu")]]
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    if action == "energy":
        await query.message.edit_text(
            "⚡️ <b>Пополнение энергии</b>\n\n"
            "Выберите готовый пакет или введите свою сумму.",
            reply_markup=energy_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "golden_card":
        active_cards = (
            sum(1 for card in user.golden_cards if card.expires_at > utcnow())
            if user
            else 0
        )
        lines = [
            "🌟 <b>Золотая карточка</b>",
            "Гарантирует закрепление вашего следующего поста в канале на выбранный срок.",
            "Позволяет выделиться в ленте и привлечь максимум внимания.",
            "",
            f"Сейчас активных карточек: <b>{active_cards}</b>",
            "",
            "Доступные варианты:",
        ]
        for hours in GOLDEN_CARD_PRESETS:
            duration = timedelta(hours=hours)
            price_usd = service.pricing.price_for_golden_card(duration)
            price_rub = service.pricing.convert_usd_to_rub(price_usd)
            energy_cost = service.energy_cost_for_golden_card(duration)
            price_text = _format_rubles(price_rub)
            note = f"~{price_text} ₽" if price_text else None
            energy_text = f" или {energy_cost}⚡️" if energy_cost else ""
            price_label = note or f"{price_usd:.2f} $"
            lines.append(f"• {hours} ч — {price_label}{energy_text}")
        await query.message.edit_text(
            "\n".join(lines),
            reply_markup=golden_card_keyboard(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    if action == "post":
        context.user_data["post_creation"] = {"step": "awaiting_content"}
        post_cost = service.post_energy_cost
        rub_note = None
        if service.current_settings and service.current_settings.energy_price_per_unit > 0:
            rub_note = service.current_settings.energy_price_per_unit * post_cost
        cost_line = f"{post_cost} ⚡️"
        if rub_note:
            cost_line += f" (~{_format_rubles(rub_note)} ₽)"
        await query.message.edit_text(
            "📝 <b>Создание поста</b>\n"
            f"Стоимость публикации составит <b>{cost_line}</b>.\n\n"
            "Отправьте текст одним сообщением.\n"
            "• Поддерживается HTML-разметка: &lt;b&gt;жирный&lt;/b&gt;, &lt;i&gt;курсив&lt;/i&gt;, &lt;u&gt;подчёркнутый&lt;/u&gt;, &lt;code&gt;код&lt;/code&gt;.\n"
            "• Чтобы добавить фото — пришлите изображение с подписью (она станет текстом).\n"
            "После текста предложу добавить кнопку.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Отмена", callback_data="action:menu")]]
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    if action == "support":
        await show_support_overview(update, context)
        return

    if action == "admin":
        if not is_admin_id(user_id, context):
            await query.answer("Доступ запрещён", show_alert=True)
            return
        await show_admin_menu(update, context)
        return

    await query.answer("Неизвестное действие", show_alert=True)


async def handle_support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    query = update.callback_query
    assert query is not None
    await query.answer()

    if not await ensure_subscription(update, context):
        return

    parts = query.data.split(":")
    if len(parts) < 2:
        await query.answer("Некорректный запрос", show_alert=True)
        return

    action = parts[1]
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None:
        await query.answer("Не удалось определить пользователя", show_alert=True)
        return

    service = get_service(context)
    user = service.get_user_balance(user_id)
    if user is None:
        await query.answer("Сначала используйте /start", show_alert=True)
        await query.message.edit_text(
            "❗️ Пользователь не найден. Нажмите /start для регистрации.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="action:menu")]]
            ),
        )
        return
    if user.is_banned and not is_admin_id(user_id, context):
        await query.answer("Ваш доступ ограничен", show_alert=True)
        return

    if action == "list":
        await show_support_overview(update, context)
        return

    if action == "new":
        context.user_data["support_new_ticket"] = True
        context.user_data.pop("support_reply", None)
        await query.message.edit_text(
            "🆘 Опишите вашу проблему одним сообщением.\n"
            "Напишите «отмена», чтобы вернуться в меню поддержки.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="support:list")]]
            ),
        )
        return

    if action == "view" and len(parts) >= 3:
        try:
            ticket_id = int(parts[2])
        except ValueError:
            await query.answer("Некорректный тикет", show_alert=True)
            return
        await show_support_ticket_detail(update, context, ticket_id)
        return

    if action == "reply" and len(parts) >= 3:
        try:
            ticket_id = int(parts[2])
        except ValueError:
            await query.answer("Некорректный тикет", show_alert=True)
            return
        ticket = service.get_ticket(ticket_id)
        if ticket is None or ticket.user_id != user_id:
            await query.answer("Тикет не найден", show_alert=True)
            return
        if ticket.status != "open":
            await query.answer("Тикет закрыт", show_alert=True)
            return
        context.user_data["support_reply"] = {"ticket_id": ticket_id}
        await query.message.edit_text(
            "✍️ Отправьте сообщение для поддержки.\n"
            "Напишите «отмена», чтобы вернуться без изменений.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data=f"support:view:{ticket_id}")]]
            ),
        )
        return

    if action in {"close", "reopen"} and len(parts) >= 3:
        try:
            ticket_id = int(parts[2])
        except ValueError:
            await query.answer("Некорректный тикет", show_alert=True)
            return
        try:
            if action == "close":
                service.close_ticket(ticket_id, actor_user_id=user_id)
                info = "✅ Тикет закрыт."
            else:
                service.reopen_ticket(ticket_id, actor_user_id=user_id)
                info = "♻️ Тикет открыт вновь."
        except PermissionError:
            await query.answer("Недостаточно прав", show_alert=True)
            return
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        await show_support_ticket_detail(update, context, ticket_id, info=info)
        return

    await query.answer("Неизвестное действие", show_alert=True)


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    if not await ensure_subscription(update, context):
        return
    service = get_service(context)
    user = service.get_user_balance(update.effective_user.id)
    if not user:
        await update.message.reply_text("Пользователь не найден. Используйте /start.")
        return
    if user.is_banned and not is_admin_id(update.effective_user.id, context):
        await update.message.reply_text("🚫 Ваш доступ к боту ограничен.")
        return
    active_cards = sum(1 for card in user.golden_cards if card.expires_at > utcnow())
    total_cards = len(user.golden_cards)
    post_cost = service.post_energy_cost
    rub_note = None
    if service.current_settings and service.current_settings.energy_price_per_unit > 0:
        rub_note = service.current_settings.energy_price_per_unit * post_cost
    cost_line = f"{post_cost} ⚡️"
    if rub_note:
        cost_line += f" (~{_format_rubles(rub_note)} ₽)"
    await update.message.reply_text(
        "\n".join(
            [
                "📊 <b>Ваш профиль</b>",
                f"⚡️ Энергия: <b>{user.energy}</b>",
                f"🌟 Активные карточки: <b>{active_cards}</b> из {total_cards}",
                f"🧾 Стоимость поста: <b>{cost_line}</b>",
            ]
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def buy_energy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    if not await ensure_subscription(update, context):
        return
    service = get_service(context)
    if not context.args:
        await update.message.reply_text("Укажите количество энергии: /buy_energy 50")
        return
    amount = int(context.args[0])
    if not is_admin_id(update.effective_user.id, context):
        await update.message.reply_text(
            "Для покупки энергии используйте кнопку «⚡️ Пополнить энергию» в меню."
        )
        return
    user = service.credit_energy(update.effective_user.id, amount)
    await update.message.reply_text(
        f"✅ Начислено {amount}⚡️. Текущий баланс: {user.energy}"
    )


async def buy_golden_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    if not await ensure_subscription(update, context):
        return
    service = get_service(context)
    if not context.args:
        await update.message.reply_text("Укажите длительность в часах: /buy_golden_card 24")
        return
    hours = int(context.args[0])
    if not is_admin_id(update.effective_user.id, context):
        await update.message.reply_text(
            "Приобретайте золотые карточки через кнопку «🌟 Золотая карточка»."
        )
        return
    service.grant_golden_card(update.effective_user.id, timedelta(hours=hours))
    await update.message.reply_text(
        f"🌟 Золотая карточка активирована на {hours} ч."
    )


async def post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    if not await ensure_subscription(update, context):
        return
    service = get_service(context)
    sync_user_profile(update, service)
    if not context.args:
        post_cost = service.post_energy_cost
        rub_note = None
        if service.current_settings and service.current_settings.energy_price_per_unit > 0:
            rub_note = service.current_settings.energy_price_per_unit * post_cost
        cost_line = f"{post_cost} ⚡️"
        if rub_note:
            cost_line += f" (~{_format_rubles(rub_note)} ₽)"
        await update.message.reply_text(
            "📝 Укажите текст поста после команды /post.\n"
            f"Стоимость публикации: {cost_line}.",
        )
        return
    message = " ".join(context.args)
    try:
        new_post = service.submit_post(update.effective_user.id, message)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    pin_text = " Пост будет закреплён." if new_post.requires_pin else ""
    await update.message.reply_text(
        f"Пост одобрен и будет отправлен в канал.{pin_text}\n"
        f"Списано {service.post_energy_cost}⚡️."
    )


async def handle_energy_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    query = update.callback_query
    assert query is not None
    await query.answer()
    if not await ensure_subscription(update, context):
        return
    try:
        data = query.data.split(":", 1)[1]
    except (ValueError, IndexError):
        await query.answer("Некорректный выбор", show_alert=True)
        return

    if data == "custom":
        context.user_data["awaiting_custom_energy"] = True
        await query.message.edit_text(
            "Введите нужное количество ⚡️ (целое число, например 150).\n"
            "Напишите «отмена», чтобы вернуться в меню.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="action:menu")]]
            ),
        )
        return

    try:
        amount = int(data)
    except ValueError:
        await query.answer("Некорректный выбор", show_alert=True)
        return

    service = get_service(context)
    sync_user_profile(update, service)
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None:
        await query.message.reply_text("Не удалось определить пользователя. Попробуйте позже.")
        return
    user = service.get_user_balance(user_id)
    if user is None:
        await query.message.reply_text("Сначала зарегистрируйтесь командой /start.")
        return
    if user.is_banned and not is_admin_id(user_id, context):
        await query.answer("Ваш доступ ограничен.", show_alert=True)
        return

    price = service.pricing.price_for_energy(amount)
    rub_total = None
    if service.current_settings:
        rub_total = service.current_settings.energy_price_per_unit * amount
    client = get_crypto_client(context)
    if client is None:
        await query.message.reply_text(
            "💤 Платёжный шлюз пока не настроен. Обратитесь к администратору."
        )
        return

    try:
        invoice = await client.create_invoice(
            amount=price,
            description=f"Пополнение энергии ({amount}⚡️)",
            payload=f"energy:{update.effective_user.id}:{amount}",
        )
    except CryptoPayError as exc:
        await query.message.reply_text(f"❌ Не удалось создать счёт: {exc}")
        return

    service.record_invoice(
        Invoice(
            invoice_id=invoice.invoice_id,
            user_id=user_id,
            invoice_type="energy",
            amount=invoice.amount,
            asset=invoice.asset,
            pay_url=invoice.pay_url,
            price=price,
            status=invoice.status,
            payload=invoice.payload,
            energy_amount=amount,
        )
    )

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Перейти к оплате", url=invoice.pay_url)],
            [
                InlineKeyboardButton(
                    "🔄 Проверить оплату", callback_data=f"invoice:check:{invoice.invoice_id}"
                )
            ],
            [InlineKeyboardButton("⬅️ В меню", callback_data="action:menu")],
        ]
    )

    amount_line = f"Сумма к оплате: {price:.2f} $"
    if rub_total is not None:
        amount_line += f" (~{rub_total:.2f} ₽)"

    await query.message.reply_text(
        "💳 Счёт для пополнения готов!\n"
        f"{amount_line}\n"
        "После оплаты нажмите «Проверить оплату», чтобы получить энергию.",
        reply_markup=keyboard,
    )


async def handle_golden_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    query = update.callback_query
    assert query is not None
    await query.answer()
    if not await ensure_subscription(update, context):
        return
    try:
        hours = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный выбор", show_alert=True)
        return

    service = get_service(context)
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None:
        await query.message.edit_text(
            "Не удалось определить пользователя. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ В меню", callback_data="action:menu")]]
            ),
        )
        return
    user = service.get_user_balance(user_id)
    if user is None:
        await query.message.edit_text(
            "Сначала зарегистрируйтесь командой /start.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ В меню", callback_data="action:menu")]]
            ),
        )
        return
    if user.is_banned and not is_admin_id(user_id, context):
        await query.answer("Ваш доступ ограничен.", show_alert=True)
        return

    duration = timedelta(hours=hours)
    price = service.pricing.price_for_golden_card(duration)
    rub_total = None
    try:
        rub_total = service.pricing.convert_usd_to_rub(price)
    except ValueError:
        rub_total = None

    energy_cost = service.energy_cost_for_golden_card(duration)

    price_line = f"💳 Стоимость: <b>{price:.2f} $</b>"
    if rub_total is not None:
        price_line += f" (~{_format_rubles(rub_total)} ₽)"

    message_lines = [
        f"🌟 <b>Золотая карточка на {hours} ч</b>",
        "Зафиксирует ваш ближайший пост в топе канала на весь выбранный срок.",
        "",
        price_line,
    ]

    if energy_cost is not None:
        message_lines.append(
            f"⚡️ Или списать <b>{energy_cost}⚡️</b> с баланса (доступно {user.energy}⚡️)."
        )

    message_lines.append("")
    message_lines.append("Выберите способ оплаты:")

    buttons = [
        [
            InlineKeyboardButton(
                "💳 Оплатить USDT", callback_data=f"goldenpay:crypto:{hours}"
            )
        ]
    ]
    if energy_cost is not None:
        buttons.append(
            [
                InlineKeyboardButton(
                    f"⚡️ Оплатить {energy_cost}⚡️",
                    callback_data=f"goldenpay:energy:{hours}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="action:golden_card")])

    await query.message.edit_text(
        "\n".join(message_lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def handle_golden_payment_selection(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    ensure_dependencies(context)
    query = update.callback_query
    assert query is not None
    await query.answer()
    if not await ensure_subscription(update, context):
        return

    try:
        _, method, hours_str = query.data.split(":", 2)
        hours = int(hours_str)
    except (ValueError, IndexError):
        await query.answer("Некорректный выбор", show_alert=True)
        return

    service = get_service(context)
    sync_user_profile(update, service)
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None:
        await query.message.edit_text(
            "Не удалось определить пользователя. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ В меню", callback_data="action:menu")]]
            ),
        )
        return

    user = service.get_user_balance(user_id)
    if user is None:
        await query.message.edit_text(
            "Сначала зарегистрируйтесь командой /start.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ В меню", callback_data="action:menu")]]
            ),
        )
        return
    if user.is_banned and not is_admin_id(user_id, context):
        await query.answer("Ваш доступ ограничен.", show_alert=True)
        return

    duration = timedelta(hours=hours)
    price = service.pricing.price_for_golden_card(duration)
    rub_total = None
    try:
        rub_total = service.pricing.convert_usd_to_rub(price)
    except ValueError:
        rub_total = None

    if method == "energy":
        energy_cost = service.energy_cost_for_golden_card(duration)
        if energy_cost is None:
            await query.answer("Оплата энергией недоступна", show_alert=True)
            return
        if user.energy < energy_cost:
            await query.answer(
                f"Недостаточно энергии. Требуется {energy_cost}⚡️, доступно {user.energy}⚡️.",
                show_alert=True,
            )
            return
        try:
            service.purchase_golden_card_with_energy(user_id, duration)
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return

        updated_user = service.get_user_balance(user_id)
        remaining = updated_user.energy if updated_user else max(0, user.energy - energy_cost)
        await query.message.edit_text(
            "✅ <b>Золотая карточка активирована!</b>\n"
            f"Списано <b>{energy_cost}⚡️</b>. Карточка действует <b>{hours} ч</b>.\n"
            f"Остаток энергии: <b>{remaining}⚡️</b>.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ В меню", callback_data="action:menu")]]
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    if method != "crypto":
        await query.answer("Неизвестный способ оплаты", show_alert=True)
        return

    client = get_crypto_client(context)
    if client is None:
        await query.message.edit_text(
            "💤 Платёжный шлюз пока не настроен. Обратитесь к администратору.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ В меню", callback_data="action:menu")]]
            ),
        )
        return

    try:
        invoice = await client.create_invoice(
            amount=price,
            description=f"Золотая карточка на {hours}ч",
            payload=f"golden:{user_id}:{hours}",
        )
    except CryptoPayError as exc:
        await query.message.edit_text(
            f"❌ Не удалось создать счёт: {exc}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ В меню", callback_data="action:menu")]]
            ),
        )
        return

    service.record_invoice(
        Invoice(
            invoice_id=invoice.invoice_id,
            user_id=user_id,
            invoice_type="golden",
            amount=invoice.amount,
            asset=invoice.asset,
            pay_url=invoice.pay_url,
            price=price,
            status=invoice.status,
            payload=invoice.payload,
            golden_hours=hours,
        )
    )

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Перейти к оплате", url=invoice.pay_url)],
            [
                InlineKeyboardButton(
                    "🔄 Проверить оплату", callback_data=f"invoice:check:{invoice.invoice_id}"
                )
            ],
            [InlineKeyboardButton("⬅️ В меню", callback_data="action:menu")],
        ]
    )

    amount_line = f"Стоимость: <b>{price:.2f} $</b>"
    if rub_total is not None:
        amount_line += f" (~{_format_rubles(rub_total)} ₽)"

    await query.message.edit_text(
        "🌟 <b>Счёт на золотую карточку готов!</b>\n"
        f"{amount_line}\n"
        "После оплаты нажмите «🔄 Проверить оплату», чтобы активировать карточку.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def handle_invoice_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    query = update.callback_query
    assert query is not None
    await query.answer()
    if not await ensure_subscription(update, context):
        return

    try:
        invoice_id = int(query.data.split(":", 2)[2])
    except (ValueError, IndexError):
        await query.answer("Некорректный номер счёта", show_alert=True)
        return

    service = get_service(context)
    sync_user_profile(update, service)
    stored_invoice = service.get_invoice(invoice_id)
    user_id = update.effective_user.id if update.effective_user else None

    if stored_invoice is None or stored_invoice.user_id != user_id:
        await query.answer("Счёт не найден", show_alert=True)
        return

    if stored_invoice.status.lower() in PAID_INVOICE_STATUSES:
        await query.answer()
        await query.message.edit_text(
            "✅ Счёт уже оплачен. Спасибо!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ В меню", callback_data="action:menu")]]
            ),
        )
        return

    client = get_crypto_client(context)
    if client is None:
        await query.answer("Платёжный шлюз не настроен", show_alert=True)
        return

    try:
        remote_invoice = await client.get_invoice(invoice_id)
    except CryptoPayError as exc:
        await query.answer(f"Ошибка проверки: {exc}", show_alert=True)
        return

    if remote_invoice.status.lower() not in PAID_INVOICE_STATUSES:
        await query.answer("Оплата ещё не подтверждена. Попробуйте позже.", show_alert=True)
        return

    service.mark_invoice_paid(invoice_id)

    if stored_invoice.invoice_type == "energy" and stored_invoice.energy_amount:
        try:
            user = service.credit_energy(stored_invoice.user_id, stored_invoice.energy_amount)
        except ValueError as exc:
            message = (
                "✅ Оплата подтверждена, но зачислить энергию не удалось:\n"
                f"{exc}"
            )
        else:
            message = (
                "✅ Оплата зафиксирована!\n"
                f"⚡️ Начислено энергии: {stored_invoice.energy_amount}\n"
                f"Текущий баланс: {user.energy}"
            )
    elif stored_invoice.invoice_type == "golden" and stored_invoice.golden_hours:
        duration = timedelta(hours=stored_invoice.golden_hours)
        try:
            service.grant_golden_card(stored_invoice.user_id, duration)
        except ValueError as exc:
            message = (
                "✅ Оплата подтверждена, но активировать золотую карточку не удалось:\n"
                f"{exc}"
            )
        else:
            message = (
                "✅ Оплата зафиксирована!\n"
                f"🌟 Золотая карточка активна на {stored_invoice.golden_hours} ч."
            )
    else:
        message = "✅ Оплата подтверждена."

    await query.answer()
    await query.message.edit_text(
        message,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ В меню", callback_data="action:menu")]]
        ),
    )


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    message = update.message
    if message is None:
        return
    if not await ensure_subscription(update, context):
        return

    service = get_service(context)
    user_data = context.user_data
    user_id = update.effective_user.id if update.effective_user else None
    user = service.get_user_balance(user_id) if user_id is not None else None
    is_admin_user = is_admin_id(user_id, context) if user_id is not None else False

    admin_reply_state = user_data.get("admin_ticket_reply")
    if admin_reply_state:
        if not is_admin_user:
            user_data.pop("admin_ticket_reply", None)
        else:
            if not message.text:
                await message.reply_text("Отправьте текст ответа или «отмена».")
                return
            raw = message.text.strip()
            ticket_id = admin_reply_state.get("ticket_id")
            if raw.lower() in {"отмена", "cancel"}:
                user_data.pop("admin_ticket_reply", None)
                await message.reply_text("Ответ отменён.")
                if ticket_id is not None:
                    await show_admin_ticket_detail(update, context, ticket_id)
                return
            if ticket_id is None:
                user_data.pop("admin_ticket_reply", None)
                await message.reply_text("Не удалось определить тикет.")
                return
            try:
                ticket = service.add_ticket_message(ticket_id, "admin", raw)
            except ValueError as exc:
                await message.reply_text(str(exc))
                return
            user_data.pop("admin_ticket_reply", None)
            await message.reply_text("Ответ отправлен пользователю.")
            try:
                await context.bot.send_message(
                    chat_id=ticket.user_id,
                    text="🆘 Поддержка: новый ответ от администратора.\n\n" + raw,
                )
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.warning("Не удалось отправить сообщение пользователю %s: %s", ticket.user_id, exc)
            await show_admin_ticket_detail(
                update,
                context,
                ticket_id,
                info="Ответ администратора отправлен.",
            )
        return

    subscription_state = user_data.get("awaiting_subscription_target")
    if subscription_state:
        if not is_admin_user:
            user_data.pop("awaiting_subscription_target", None)
        else:
            if not message.text:
                await message.reply_text(
                    "Отправьте @username, числовой ID или ссылку, либо «отмена»."
                )
                return
            raw = message.text.strip()
            if raw.lower() in {"отмена", "cancel"}:
                user_data.pop("awaiting_subscription_target", None)
                await message.reply_text("Изменение ссылки отменено.")
                await show_admin_menu(update, context)
                return
            try:
                chat_id, invite_link = _parse_subscription_input(raw)
            except ValueError as exc:
                await message.reply_text(str(exc))
                return
            service.update_subscription_requirement(chat_id, invite_link)
            user_data.pop("awaiting_subscription_target", None)
            await message.reply_text("✅ Требование подписки обновлено.")
            await show_admin_menu(
                update,
                context,
                info="Настройки подписки обновлены.",
            )
        return

    support_new_state = user_data.get("support_new_ticket")
    if support_new_state:
        if not message.text:
            await message.reply_text("Опишите вопрос текстом или напишите «отмена».")
            return
        raw = message.text.strip()
        if raw.lower() in {"отмена", "cancel"}:
            user_data.pop("support_new_ticket", None)
            await message.reply_text("Создание тикета отменено.")
            await show_support_overview(update, context)
            return
        if user_id is None:
            user_data.pop("support_new_ticket", None)
            await message.reply_text("Не удалось определить пользователя.")
            return
        try:
            ticket = service.open_ticket(user_id, raw)
        except ValueError as exc:
            await message.reply_text(str(exc))
            return
        user_data.pop("support_new_ticket", None)
        await show_support_ticket_detail(
            update,
            context,
            ticket.ticket_id,
            info="✅ Тикет создан. Ожидайте ответа администратора.",
        )
        return

    support_reply_state = user_data.get("support_reply")
    if support_reply_state:
        if not message.text:
            await message.reply_text("Отправьте текст сообщения или «отмена».")
            return
        raw = message.text.strip()
        ticket_id = support_reply_state.get("ticket_id")
        if raw.lower() in {"отмена", "cancel"}:
            user_data.pop("support_reply", None)
            if ticket_id is not None:
                await show_support_ticket_detail(update, context, ticket_id)
            else:
                await show_support_overview(update, context)
            return
        if ticket_id is None:
            user_data.pop("support_reply", None)
            await message.reply_text("Не удалось определить тикет.")
            return
        try:
            service.add_ticket_message(ticket_id, "user", raw)
        except ValueError as exc:
            await message.reply_text(str(exc))
            return
        user_data.pop("support_reply", None)
        await show_support_ticket_detail(
            update,
            context,
            ticket_id,
            info="✉️ Сообщение отправлено. Мы ответим вам как можно скорее.",
        )
        return

    # Admin adjusts post price
    if user_data.get("awaiting_post_price"):
        if not message.text:
            await message.reply_text("Отправьте целое число, например 25.")
            return
        raw = message.text.strip()
        if raw.lower() in {"отмена", "cancel"}:
            user_data.pop("awaiting_post_price", None)
            await message.reply_text("Изменение стоимости поста отменено.")
            await show_admin_menu(update, context)
            return
        try:
            cost = int(raw)
            if cost <= 0:
                raise ValueError
        except ValueError:
            await message.reply_text("Введите целое число больше 0, например 25.")
            return
        user_data.pop("awaiting_post_price", None)
        try:
            settings = service.update_post_price(cost)
        except ValueError as exc:
            await message.reply_text(str(exc))
            return
        await message.reply_text(
            f"Стоимость поста обновлена: {settings.post_energy_cost} ⚡️."
        )
        await show_admin_menu(update, context, info="Стоимость поста обновлена.")
        return

    # Admin adjusts energy price
    if user_data.get("awaiting_energy_price"):
        if not message.text:
            await message.reply_text("Отправьте число, например 15.5.")
            return
        raw = message.text.strip()
        if raw.lower() in {"отмена", "cancel"}:
            user_data.pop("awaiting_energy_price", None)
            await message.reply_text("Изменение цены энергии отменено.")
            await show_admin_menu(update, context)
            return
        try:
            price = float(raw.replace(",", "."))
            if price <= 0:
                raise ValueError
        except ValueError:
            await message.reply_text("Введите положительное число, например 15.5.")
            return
        user_data.pop("awaiting_energy_price", None)
        try:
            settings = service.update_energy_price(price)
        except ValueError as exc:
            await message.reply_text(str(exc))
            return
        await message.reply_text(
            f"Цена за энергию обновлена: {settings.energy_price_per_unit:.2f} ₽ за 1 ⚡️."
        )
        await show_admin_menu(update, context, info="Цена за энергию обновлена.")
        return

    balance_state = user_data.get("awaiting_user_balance")
    if balance_state:
        if not message.text:
            await message.reply_text("Введите новое значение или «отмена».")
            return
        raw = message.text.strip()
        target_user_id = balance_state.get("user_id")
        if raw.lower() in {"отмена", "cancel"}:
            user_data.pop("awaiting_user_balance", None)
            await message.reply_text("Изменение баланса отменено.")
            if target_user_id is not None:
                await show_user_detail(update, context, target_user_id)
            return
        if target_user_id is None:
            user_data.pop("awaiting_user_balance", None)
            await message.reply_text("Не удалось определить пользователя.")
            return
        try:
            if raw.startswith(("+", "-")):
                delta = int(raw)
                target_user = service.adjust_user_energy(target_user_id, delta)
                notice = f"Баланс изменён на {delta:+d}⚡️. Новый баланс: {target_user.energy}⚡️."
            else:
                value = int(raw)
                target_user = service.set_user_energy(target_user_id, value)
                notice = f"Баланс установлен: {target_user.energy}⚡️."
        except ValueError as exc:
            await message.reply_text(f"Ошибка: {exc}")
            return
        user_data.pop("awaiting_user_balance", None)
        await show_user_detail(update, context, target_user_id, notice=notice)
        return

    if user and user.is_banned and not is_admin_user:
        user_data.pop("post_creation", None)
        user_data.pop("awaiting_custom_energy", None)
        await message.reply_text("🚫 Ваш доступ к боту ограничен.")
        return

    # Custom energy purchase
    if user_data.get("awaiting_custom_energy"):
        if not message.text:
            await message.reply_text("Введите количество ⚡️ цифрами или напишите «отмена».")
            return
        raw = message.text.strip()
        if raw.lower() in {"отмена", "cancel"}:
            user_data.pop("awaiting_custom_energy", None)
            await send_main_menu(update, context, "Выберите действие из меню 👇")
            return
        try:
            amount = int(raw)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await message.reply_text("Укажите целое число больше 0, например 150.")
            return
        if user_id is None:
            user_data.pop("awaiting_custom_energy", None)
            await message.reply_text("Не удалось определить пользователя. Попробуйте позже.")
            return
        user = service.get_user_balance(user_id)
        if user is None:
            user_data.pop("awaiting_custom_energy", None)
            await message.reply_text("Сначала зарегистрируйтесь командой /start.")
            return
        if user.is_banned and not is_admin_user:
            user_data.pop("awaiting_custom_energy", None)
            await message.reply_text("🚫 Ваш доступ к боту ограничен.")
            return
        price = service.pricing.price_for_energy(amount)
        rub_total = None
        if service.current_settings:
            rub_total = service.current_settings.energy_price_per_unit * amount
        client = get_crypto_client(context)
        if client is None:
            user_data.pop("awaiting_custom_energy", None)
            await message.reply_text(
                "💤 Платёжный шлюз пока не настроен. Обратитесь к администратору."
            )
            return
        try:
            invoice = await client.create_invoice(
                amount=price,
                description=f"Пополнение энергии ({amount}⚡️)",
                payload=f"energy:{user_id}:{amount}",
            )
        except CryptoPayError as exc:
            await message.reply_text(f"❌ Не удалось создать счёт: {exc}")
            return

        service.record_invoice(
            Invoice(
                invoice_id=invoice.invoice_id,
                user_id=user_id,
                invoice_type="energy",
                amount=invoice.amount,
                asset=invoice.asset,
                pay_url=invoice.pay_url,
                price=price,
                status=invoice.status,
                payload=invoice.payload,
                energy_amount=amount,
            )
        )

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💳 Перейти к оплате", url=invoice.pay_url)],
                [
                    InlineKeyboardButton(
                        "🔄 Проверить оплату", callback_data=f"invoice:check:{invoice.invoice_id}"
                    )
                ],
                [InlineKeyboardButton("⬅️ В меню", callback_data="action:menu")],
            ]
        )

        amount_line = f"Сумма к оплате: {price:.2f} $"
        if rub_total is not None:
            amount_line += f" (~{rub_total:.2f} ₽)"

        await message.reply_text(
            "💳 Счёт для пополнения готов!\n"
            f"{amount_line}\n"
            "После оплаты нажмите «Проверить оплату», чтобы получить энергию.",
            reply_markup=keyboard,
        )
        user_data.pop("awaiting_custom_energy", None)
        return

    # Post creation workflow
    post_state = user_data.get("post_creation")
    if isinstance(post_state, dict):
        step = post_state.get("step", "awaiting_content")
        if step == "awaiting_content":
            photo_file_id = None
            if message.photo:
                photo_file_id = message.photo[-1].file_id
                content_text = (message.caption or "").strip()
                if not content_text:
                    await message.reply_text(
                        "Добавьте подпись к фото — она станет текстом поста."
                    )
                    return
            else:
                content_text = (message.text or "").strip() if message.text else ""
                if not content_text:
                    await message.reply_text(
                        "Сообщение не должно быть пустым. Попробуйте ещё раз ✍️"
                    )
                    return

            post_state.update(
                {
                    "text": content_text,
                    "photo_file_id": photo_file_id,
                    "parse_mode": "HTML",
                    "step": "awaiting_button",
                }
            )
            user_data["post_creation"] = post_state
            await message.reply_text(
                "Добавим кнопку? Отправьте текст в формате:\n"
                "Название кнопки | https://example.com\n"
                "Или напишите «пропустить», если кнопка не нужна.",
            )
            return

        if step == "awaiting_button":
            if not message.text:
                await message.reply_text(
                    "Отправьте кнопку в формате «Название | https://пример» или «пропустить»."
                )
                return
            normalized = message.text.strip()
            lower = normalized.lower()
            if lower in {"пропустить", "skip"}:
                button_text = None
                button_url = None
            else:
                if "|" not in normalized:
                    await message.reply_text(
                        "Укажите кнопку в формате «Название | https://пример»."
                    )
                    return
                title, url = (part.strip() for part in normalized.split("|", 1))
                if not title or not url:
                    await message.reply_text("Текст и ссылка кнопки не должны быть пустыми.")
                    return
                if not url.lower().startswith(("http://", "https://")):
                    await message.reply_text(
                        "Ссылка должна начинаться с http:// или https://."
                    )
                    return
                button_text = title
                button_url = url
            try:
                new_post = service.submit_post(
                    update.effective_user.id,
                    post_state["text"],
                    button_text=button_text,
                    button_url=button_url,
                    photo_file_id=post_state.get("photo_file_id"),
                    parse_mode=post_state.get("parse_mode"),
                )
            except ValueError as exc:
                await message.reply_text(f"❌ {exc}")
                return

            user_data.pop("post_creation", None)
            pin_text = " 📌 Пост будет закреплён." if new_post.requires_pin else ""
            button_note = (
                f"\n🔗 Кнопка: {button_text} → {button_url}"
                if button_text and button_url
                else ""
            )
            await message.reply_text(
                f"✅ Пост принят и отправлен на модерацию!{pin_text}{button_note}\n"
                f"Списано {service.post_energy_cost} ⚡️."
            )
            await send_main_menu(update, context, "Выберите следующее действие 👇")
            return

    if user is None:
        await message.reply_text("Пожалуйста, зарегистрируйтесь командой /start.")
        return

    # Fallback response
    await message.reply_text(
        "🙌 Используйте кнопки ниже для управления ботом.",
        reply_markup=main_menu_keyboard(is_admin=is_admin_user),
    )


async def autopost_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    service = get_service(context)

    if service.is_autopost_paused():
        return

    post = service.reserve_next_post()
    if not post:
        return

    if post.post_id is None:
        LOGGER.warning("Получен пост без идентификатора, возвращаем в очередь")
        return

    if TELEGRAM_CHANNEL_ID is None:
        LOGGER.warning("TELEGRAM_CHANNEL_ID не задан. Автопостинг приостановлен.")
        service.set_autopost_paused(True)
        service.mark_post_failed(post.post_id)
        return

    keyboard = None
    if post.button_text and post.button_url:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(post.button_text, url=post.button_url)]]
        )

    try:
        if post.photo_file_id:
            channel_message = await context.bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=post.photo_file_id,
                caption=post.text,
                parse_mode=post.parse_mode,
                reply_markup=keyboard,
            )
        else:
            channel_message = await context.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=post.text,
                parse_mode=post.parse_mode,
                reply_markup=keyboard,
            )

        chat_message_id = None

        if TELEGRAM_CHAT_ID is not None:
            if post.photo_file_id:
                chat_message = await context.bot.send_photo(
                    chat_id=TELEGRAM_CHAT_ID,
                    photo=post.photo_file_id,
                    caption=post.text,
                    parse_mode=post.parse_mode,
                    reply_markup=keyboard,
                )
            else:
                chat_message = await context.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=post.text,
                    parse_mode=post.parse_mode,
                    reply_markup=keyboard,
                )
            chat_message_id = chat_message.message_id

        if post.requires_pin:
            try:
                await context.bot.pin_chat_message(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    message_id=channel_message.message_id,
                    disable_notification=True,
                )
            except Exception as exc:  # pylint: disable=broad-except
                LOGGER.warning("Не удалось закрепить пост %s: %s", post.post_id, exc)

        service.mark_post_published(
            post.post_id,
            channel_message_id=channel_message.message_id,
            chat_message_id=chat_message_id,
        )
    except Exception as exc:  # pylint: disable=broad-except
        LOGGER.exception("Ошибка автопубликации поста %s: %s", post.post_id, exc)
        service.mark_post_failed(post.post_id)


def admin_menu_keyboard(paused: bool) -> InlineKeyboardMarkup:
    toggle_label = "▶️ Возобновить автопостинг" if paused else "⏸ Поставить на паузу"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👥 Пользователи", callback_data="admin:users"),
                InlineKeyboardButton("📊 Статистика", callback_data="admin:stats"),
            ],
            [InlineKeyboardButton(toggle_label, callback_data="admin:toggle_pause")],
            [
                InlineKeyboardButton("💰 Финансы", callback_data="admin:finance"),
                InlineKeyboardButton("🗂 Заявки", callback_data="admin:requests"),
            ],
            [
                InlineKeyboardButton("🆘 Поддержка", callback_data="admin:support"),
                InlineKeyboardButton("⚙️ Цены", callback_data="admin:prices"),
            ],
            [
                InlineKeyboardButton("🔗 Подписка", callback_data="admin:subscription"),
                InlineKeyboardButton("💳 CryptoPay", callback_data="admin:cryptopay"),
            ],
            [InlineKeyboardButton("🔄 Обновить", callback_data="admin:refresh")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="action:menu")],
        ]
    )


async def show_users_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0
) -> None:
    service = get_service(context)
    users = sorted(service.list_users(), key=lambda u: u.user_id)
    total = len(users)
    if total == 0:
        text = "👥 Пользователей пока нет."
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎛 Админ-панель", callback_data="admin:refresh")]]
        )
        await update.callback_query.message.edit_text(text, reply_markup=keyboard)
        return

    max_page = (total - 1) // ADMIN_USERS_PAGE_SIZE
    page = max(0, min(page, max_page))
    context.user_data["admin_users_page"] = page
    start = page * ADMIN_USERS_PAGE_SIZE
    subset = users[start : start + ADMIN_USERS_PAGE_SIZE]

    admin_count = sum(1 for u in users if u.is_admin)
    banned_count = sum(1 for u in users if u.is_banned)
    lines = [
        "👥 Пользователи",
        f"Всего: {total} • Админов: {admin_count} • Забанено: {banned_count}",
        "",
    ]
    for user in subset:
        tags = []
        if user.is_admin:
            tags.append("admin")
        if user.is_banned:
            tags.append("ban")
        tag_str = f" [{' • '.join(tags)}]" if tags else ""
        username_display = f"@{user.username}" if user.username else "—"
        name_display = user.full_name or "—"
        lines.append(f"• {user.user_id} — {user.energy}⚡️{tag_str}")
        lines.append(f"  {username_display} • {name_display}")
    lines.append(f"\nСтраница {page + 1} из {max_page + 1}")

    buttons = [
        [
            InlineKeyboardButton(
                f"{user.user_id} • {user.energy}⚡️",
                callback_data=f"admin:user:{user.user_id}",
            )
        ]
        for user in subset
    ]

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:users:{page - 1}")
        )
    if page < max_page:
        nav.append(
            InlineKeyboardButton("➡️ Далее", callback_data=f"admin:users:{page + 1}")
        )
    if nav:
        buttons.append(nav)
    buttons.append(
        [InlineKeyboardButton("🎛 Админ-панель", callback_data="admin:refresh")]
    )

    text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=keyboard)
    elif update.message:
        await update.message.reply_text(text, reply_markup=keyboard)


async def show_user_detail(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target_user_id: int,
    *,
    notice: str | None = None,
) -> None:
    service = get_service(context)
    user = service.get_user_balance(target_user_id)
    if not user:
        await update.callback_query.message.edit_text(
            "❗️ Пользователь не найден.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Пользователи", callback_data="admin:users")]]
            ),
        )
        return

    pending_posts = service.list_posts_for_user(user.user_id, ["pending"])
    approved_posts = service.list_posts_for_user(user.user_id, ["approved", "publishing"])

    username_display = f"@{user.username}" if user.username else "—"
    name_display = user.full_name or "—"
    lines = [
        f"👤 Пользователь {user.user_id}",
        f"• Username: {username_display}",
        f"• Имя: {name_display}",
        f"• ⚡️ Баланс: {user.energy}",
        f"• 🌟 Золотых карточек: {len(user.golden_cards)}",
        f"• Статус: {'🚫 Забанен' if user.is_banned else '✅ Активен'}",
        f"• Права: {'👑 Администратор' if user.is_admin else '🙋‍♂️ Пользователь'}",
        f"• Постов в ожидании: {len(pending_posts)}",
        f"• Постов в очереди: {len(approved_posts)}",
    ]
    if notice:
        lines.append(f"\nℹ️ {notice}")

    buttons: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                "⚙️ Изменить баланс",
                callback_data=f"admin:user:{user.user_id}:balance",
            )
        ],
        [
            InlineKeyboardButton(
                "👑 Снять админку" if user.is_admin else "👑 Выдать админку",
                callback_data=f"admin:user:{user.user_id}:toggle_admin",
            )
        ],
        [
            InlineKeyboardButton(
                "🚫 Разбанить" if user.is_banned else "🚫 Забанить",
                callback_data=f"admin:user:{user.user_id}:toggle_ban",
            )
        ],
        [
            InlineKeyboardButton(
                "🧹 Снять активные посты",
                callback_data=f"admin:user:{user.user_id}:clear_posts",
            )
        ],
    ]

    current_page = context.user_data.get("admin_users_page", 0)
    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ К пользователям", callback_data=f"admin:users:{current_page}"
            )
        ]
    )
    buttons.append(
        [InlineKeyboardButton("🎛 Админ-панель", callback_data="admin:refresh")]
    )

    await update.callback_query.message.edit_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_request_detail(
    update: Update, context: ContextTypes.DEFAULT_TYPE, index: int = 0
) -> None:
    service = get_service(context)
    pending_posts = service.list_pending_posts()
    if not pending_posts:
        await update.callback_query.message.edit_text(
            "🗂 Заявок на посты нет.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🎛 Админ-панель", callback_data="admin:refresh")]]
            ),
        )
        return

    max_index = len(pending_posts) - 1
    index = max(0, min(index, max_index))
    context.user_data["admin_requests_index"] = index
    post = pending_posts[index]
    author = service.get_user_balance(post.user_id)

    preview = post.text.strip()
    if len(preview) > POST_PREVIEW_LENGTH:
        preview = preview[: POST_PREVIEW_LENGTH - 3] + "..."

    lines = [
        f"🗂 Заявка #{post.post_id} — пользователь {post.user_id}",
        f"Статус: {post.status}",
        f"Баланс автора: {author.energy if author else '—'} ⚡️",
    ]
    if post.button_text and post.button_url:
        lines.append(f"Кнопка: {post.button_text} → {post.button_url}")
    lines.append("\nТекст поста:\n")
    lines.append(preview or "—")
    lines.append(f"\nЗаявка {index + 1} из {len(pending_posts)}")
    lines.append(
        "\nПривязанные каналы:\n"
        f"• Канал: {TELEGRAM_CHANNEL_ID or 'не настроен'}\n"
        f"• Чат: {TELEGRAM_CHAT_ID or 'не настроен'}"
    )

    buttons = [
        [
            InlineKeyboardButton(
                "✅ Принять",
                callback_data=f"admin:requests:approve:{post.post_id}:{index}",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Отклонить",
                callback_data=f"admin:requests:reject:{post.post_id}:{index}",
            )
        ],
        [
            InlineKeyboardButton(
                "👤 Автор",
                callback_data=f"admin:user:{post.user_id}",
            )
        ],
    ]

    nav: list[InlineKeyboardButton] = []
    if index > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Предыдущая", callback_data=f"admin:requests:view:{index - 1}"
            )
        )
    if index < max_index:
        nav.append(
            InlineKeyboardButton(
                "➡️ Следующая", callback_data=f"admin:requests:view:{index + 1}"
            )
        )
    if nav:
        buttons.append(nav)
    buttons.append(
        [InlineKeyboardButton("🎛 Админ-панель", callback_data="admin:refresh")]
    )

    await update.callback_query.message.edit_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_admin_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    info: str | None = None,
) -> None:
    ensure_dependencies(context)
    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin_id(user_id, context):
        if update.callback_query:
            await update.callback_query.answer("Доступ запрещён", show_alert=True)
        else:
            await update.message.reply_text("Доступ запрещён.")
        return

    if not await ensure_subscription(update, context):
        return

    service = get_service(context)
    paused = service.is_autopost_paused()
    header = "🎛 Админ-панель\n\n"
    body = info or "Выберите раздел для управления ботом."
    text = header + body
    keyboard = admin_menu_keyboard(paused)

    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=keyboard)
    elif update.message:
        await update.message.reply_text(text, reply_markup=keyboard)


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    query = update.callback_query
    assert query is not None
    await query.answer()

    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin_id(user_id, context):
        await query.answer("Доступ запрещён", show_alert=True)
        return

    if not await ensure_subscription(update, context):
        return

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    args = parts[2:]
    service = get_service(context)

    if action == "users":
        page = int(args[0]) if args else context.user_data.get("admin_users_page", 0)
        await show_users_page(update, context, page)
    elif action == "user":
        if not args:
            await show_users_page(update, context, context.user_data.get("admin_users_page", 0))
            return
        try:
            target_id = int(args[0])
        except ValueError:
            await query.answer("Некорректный пользователь", show_alert=True)
            return
        if len(args) == 1:
            await show_user_detail(update, context, target_id)
            return
        subaction = args[1]
        if subaction == "balance":
            context.user_data["awaiting_user_balance"] = {"user_id": target_id}
            await query.message.edit_text(
                "Введите новый баланс в ⚡️ или изменение с префиксом +/-, например «150» или «+20».\n"
                "Напишите «отмена», чтобы выйти без изменений.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ Назад",
                                callback_data=f"admin:user:{target_id}",
                            )
                        ]
                    ]
                ),
            )
        elif subaction == "toggle_admin":
            user = service.get_user_balance(target_id)
            if not user:
                await query.answer("Пользователь не найден", show_alert=True)
                return
            new_state = not user.is_admin
            service.set_user_admin(target_id, new_state)
            await query.answer(
                "Админ-права выданы" if new_state else "Админ-права сняты",
                show_alert=True,
            )
            await show_user_detail(
                update,
                context,
                target_id,
                notice="Права администратора обновлены.",
            )
        elif subaction == "toggle_ban":
            user = service.get_user_balance(target_id)
            if not user:
                await query.answer("Пользователь не найден", show_alert=True)
                return
            new_state = not user.is_banned
            service.set_user_banned(target_id, new_state)
            removed = 0
            if new_state:
                removed = service.cancel_posts_for_user(target_id)
            await query.answer(
                "Пользователь заблокирован" if new_state else "Пользователь разблокирован",
                show_alert=True,
            )
            notice = (
                "Пользователь заблокирован. Активные посты удалены."
                if new_state and removed
                else (
                    "Пользователь разблокирован."
                    if not new_state
                    else "Пользователь заблокирован."
                )
            )
            await show_user_detail(update, context, target_id, notice=notice)
        elif subaction == "clear_posts":
            removed = service.cancel_posts_for_user(target_id)
            await query.answer(f"Удалено {removed} постов.", show_alert=True)
            await show_user_detail(
                update,
                context,
                target_id,
                notice=f"Удалено {removed} активных пост(ов).",
            )
        else:
            await query.answer("Неизвестное действие", show_alert=True)
    elif action == "stats":
        stats = service.get_statistics()
        info = (
            "📊 Статистика\n"
            f"• Пользователи: {stats['users']}\n"
            f"• Постов в очереди: {stats['posts_pending']}\n"
            f"• Опубликовано: {stats['posts_published']}\n"
            f"• Всего постов: {stats['posts_total']}"
        )
        await show_admin_menu(update, context, info=info)
    elif action == "toggle_pause":
        paused = service.is_autopost_paused()
        service.set_autopost_paused(not paused)
        info = (
            "▶️ Автопостинг возобновлён."
            if paused
            else "⏸ Автопостинг поставлен на паузу."
        )
        await show_admin_menu(update, context, info=info)
    elif action == "finance":
        summary = service.get_finance_summary()
        info = (
            "💰 Финансы\n"
            f"• Всего счетов: {summary['invoices_total']}\n"
            f"• Оплачено: {summary['invoices_paid']}\n"
            f"• Ожидает оплаты: {summary['invoices_pending']}\n"
            f"• Получено средств: {summary['revenue_collected']:.2f}\n"
            f"• В ожидании: {summary['revenue_waiting']:.2f}"
        )
        await show_admin_menu(update, context, info=info)
    elif action == "subscription":
        settings = service.get_settings()
        context.user_data.pop("awaiting_subscription_target", None)
        requirement = settings.subscription_chat_id
        link = settings.subscription_invite_link or _subscription_link(settings)
        lines = ["🔗 <b>Проверка подписки</b>"]
        if requirement:
            lines.append(
                f"• Проверяется подписка на: <code>{html.escape(str(requirement))}</code>"
            )
            if link:
                safe_link = html.escape(link, quote=True)
                lines.append(f"• Ссылка для пользователей: <a href=\"{safe_link}\">{safe_link}</a>")
            lines.append("")
            lines.append(
                "Пользователи должны быть подписаны на канал, чтобы пользоваться ботом."
            )
        else:
            lines.append("• Проверка подписки <b>отключена</b>.")
            lines.append("")
            lines.append("Укажите канал, чтобы активировать проверку.")
        lines.append("")
        lines.append(
            "Отправьте @username, числовой ID или ссылку формата https://t.me/... для обновления."
        )
        keyboard_rows = [
            [
                InlineKeyboardButton(
                    "✏️ Изменить ссылку", callback_data="admin:set_subscription"
                )
            ]
        ]
        if requirement:
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        "🚫 Отключить проверку", callback_data="admin:clear_subscription"
                    )
                ]
            )
        keyboard_rows.append(
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin:refresh")]
        )
        await query.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    elif action == "set_subscription":
        context.user_data["awaiting_subscription_target"] = True
        await query.message.edit_text(
            "Отправьте @username или числовой ID канала.\n"
            "Можно указать ссылку после пробела: «@channel https://t.me/channel».\n"
            "Напишите «отмена», чтобы выйти без изменений.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="admin:subscription")]]
            ),
        )
    elif action == "clear_subscription":
        service.update_subscription_requirement(None, None)
        await show_admin_menu(update, context, info="✅ Проверка подписки отключена.")
    elif action == "prices":
        settings = service.get_settings()
        info = (
            "⚙️ Настройки цен\n"
            f"• Стоимость публикации: {settings.post_energy_cost} ⚡️\n"
            f"• Цена 1 ⚡️: {settings.energy_price_per_unit:.2f} ₽\n\n"
            "Выберите параметр для изменения."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Изменить стоимость поста", callback_data="admin:set_post_price")],
                [InlineKeyboardButton("Изменить цену энергии", callback_data="admin:set_energy_price")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="admin:refresh")],
            ]
        )
        await query.message.edit_text(info, reply_markup=keyboard)
    elif action == "set_post_price":
        context.user_data["awaiting_post_price"] = True
        context.user_data.pop("awaiting_energy_price", None)
        await query.message.edit_text(
            "Введите новую стоимость поста в ⚡️ (целое число больше 0).\n"
            "Напишите «отмена», чтобы выйти без изменений.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="admin:prices")]]
            ),
        )
    elif action == "set_energy_price":
        context.user_data["awaiting_energy_price"] = True
        context.user_data.pop("awaiting_post_price", None)
        await query.message.edit_text(
            "Введите цену за 1 ⚡️ в рублях (например, 15.5).\n"
            "Напишите «отмена», чтобы выйти без изменений.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="admin:prices")]]
            ),
        )
    elif action == "requests":
        if not args:
            current = context.user_data.get("admin_requests_index", 0)
            await show_request_detail(update, context, current)
            return
        subaction = args[0]
        if subaction == "view":
            index = int(args[1]) if len(args) > 1 else context.user_data.get("admin_requests_index", 0)
            await show_request_detail(update, context, index)
        elif subaction in {"approve", "reject"} and len(args) >= 3:
            try:
                post_id = int(args[1])
                index = int(args[2])
            except ValueError:
                await query.answer("Некорректные данные", show_alert=True)
                return
            if subaction == "approve":
                result = service.approve_post(post_id)
                if result is None:
                    await query.answer("Заявку одобрить не удалось.", show_alert=True)
                else:
                    await query.answer("Заявка одобрена", show_alert=True)
            else:
                result = service.reject_post(post_id)
                if result is None:
                    await query.answer("Заявку отклонить не удалось.", show_alert=True)
                else:
                    await query.answer("Заявка отклонена", show_alert=True)
            await show_request_detail(update, context, index)
        else:
            await query.answer("Неизвестное действие", show_alert=True)
    elif action == "support":
        if not args:
            await show_admin_support_overview(update, context)
            return
        subaction = args[0]
        if subaction == "filter" and len(args) >= 2:
            target = args[1]
            if target not in {"open", "all"}:
                await query.answer("Некорректный фильтр", show_alert=True)
                return
            context.user_data["admin_support_filter"] = target
            context.user_data["admin_support_page"] = 0
            await show_admin_support_overview(update, context)
        elif subaction == "page" and len(args) >= 2:
            try:
                page = int(args[1])
            except ValueError:
                await query.answer("Некорректная страница", show_alert=True)
                return
            context.user_data["admin_support_page"] = page
            await show_admin_support_overview(update, context)
        elif subaction == "view" and len(args) >= 2:
            try:
                ticket_id = int(args[1])
            except ValueError:
                await query.answer("Некорректный тикет", show_alert=True)
                return
            await show_admin_ticket_detail(update, context, ticket_id)
        elif subaction == "reply" and len(args) >= 2:
            try:
                ticket_id = int(args[1])
            except ValueError:
                await query.answer("Некорректный тикет", show_alert=True)
                return
            ticket = service.get_ticket(ticket_id)
            if ticket is None:
                await query.answer("Тикет не найден", show_alert=True)
                return
            if ticket.status != "open":
                await query.answer("Тикет закрыт", show_alert=True)
                return
            context.user_data["admin_ticket_reply"] = {"ticket_id": ticket_id}
            await query.message.edit_text(
                "✍️ Отправьте ответ пользователю одним сообщением.\n"
                "Напишите «отмена», чтобы выйти без изменений.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:support:view:{ticket_id}")]]
                ),
            )
        elif subaction in {"close", "reopen"} and len(args) >= 2:
            try:
                ticket_id = int(args[1])
            except ValueError:
                await query.answer("Некорректный тикет", show_alert=True)
                return
            try:
                if subaction == "close":
                    service.close_ticket(ticket_id)
                    info = "✅ Тикет закрыт."
                else:
                    service.reopen_ticket(ticket_id)
                    info = "♻️ Тикет открыт вновь."
            except ValueError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            await show_admin_ticket_detail(update, context, ticket_id, info=info)
        else:
            await query.answer("Неизвестное действие", show_alert=True)
    elif action == "cryptopay":
        token_configured = bool(os.environ.get("CRYPTOPAY_TOKEN"))
        info = (
            "💳 CryptoPay\n"
            f"• Токен настроен: {'✅' if token_configured else '❌'}\n"
            f"• Последние счета создаются через API CryptoBot.\n"
            "• Используйте раздел «Проверить оплату», чтобы синхронизировать оплаты."
        )
        await show_admin_menu(update, context, info=info)
    elif action == "refresh":
        await show_admin_menu(update, context, info="🔄 Данные обновлены.")
    else:
        await query.answer("Неизвестный раздел", show_alert=True)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    if not await ensure_subscription(update, context):
        return
    await show_admin_menu(update, context)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required")
    application = (
        ApplicationBuilder()
        .token(token)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("buy_energy", buy_energy))
    application.add_handler(CommandHandler("buy_golden_card", buy_golden_card))
    application.add_handler(CommandHandler("post", post))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^admin:"))
    application.add_handler(CallbackQueryHandler(handle_support_callback, pattern="^support:"))
    application.add_handler(CallbackQueryHandler(handle_invoice_check, pattern="^invoice:check:"))
    application.add_handler(CallbackQueryHandler(handle_menu_action, pattern="^action:"))
    application.add_handler(CallbackQueryHandler(handle_energy_selection, pattern="^energy:"))
    application.add_handler(
        CallbackQueryHandler(handle_golden_payment_selection, pattern="^goldenpay:")
    )
    application.add_handler(CallbackQueryHandler(handle_golden_selection, pattern="^golden:"))
    application.add_handler(
        MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, handle_user_message)
    )

    job_queue = application.job_queue
    job_queue.run_repeating(
        autopost_job,
        interval=AUTOPOST_INTERVAL_SECONDS,
        first=10,
        name="autopost",
    )

    LOGGER.info(
        "Bot started (autopost interval: %ss, channel: %s, chat: %s)",
        AUTOPOST_INTERVAL_SECONDS,
        TELEGRAM_CHANNEL_ID,
        TELEGRAM_CHAT_ID,
    )
    application.run_polling(close_loop=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
def admin_menu_keyboard(paused: bool) -> InlineKeyboardMarkup:
    toggle_label = "▶️ Возобновить автопостинг" if paused else "⏸ Поставить на паузу"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👥 Пользователи", callback_data="admin:users"),
                InlineKeyboardButton("📊 Статистика", callback_data="admin:stats"),
            ],
            [InlineKeyboardButton(toggle_label, callback_data="admin:toggle_pause")],
            [
                InlineKeyboardButton("💰 Финансы", callback_data="admin:finance"),
                InlineKeyboardButton("🗂 Заявки", callback_data="admin:requests"),
            ],
            [
                InlineKeyboardButton("🔗 Подписка", callback_data="admin:subscription"),
                InlineKeyboardButton("💳 CryptoPay", callback_data="admin:cryptopay"),
            ],
            [InlineKeyboardButton("🔄 Обновить", callback_data="admin:refresh")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="action:menu")],
        ]
    )


async def show_admin_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    info: str | None = None,
) -> None:
    ensure_dependencies(context)
    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin_id(user_id, context):
        if update.callback_query:
            await update.callback_query.answer("Доступ запрещён", show_alert=True)
        else:
            await update.message.reply_text("Доступ запрещён.")
        return

    if not await ensure_subscription(update, context):
        return

    service = get_service(context)
    paused = service.is_autopost_paused()
    header = "🎛 Админ-панель\n\n"
    body = info or "Выберите раздел для управления ботом."
    text = header + body
    keyboard = admin_menu_keyboard(paused)

    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=keyboard)
    elif update.message:
        await update.message.reply_text(text, reply_markup=keyboard)


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    query = update.callback_query
    assert query is not None
    await query.answer()

    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin_id(user_id, context):
        await query.answer("Доступ запрещён", show_alert=True)
        return

    if not await ensure_subscription(update, context):
        return

    action = query.data.split(":", 1)[1]
    service = get_service(context)

    if action == "users":
        users = service.list_users()
        total = len(users)
        referrals = sum(len(user.referred_users) for user in users)
        avg_energy = sum(user.energy for user in users) / total if total else 0
        info = (
            "👥 Пользователи\n"
            f"• Всего: {total}\n"
            f"• Рефералов привлечено: {referrals}\n"
            f"• Средний запас энергии: {avg_energy:.1f} ⚡️"
        )
        await show_admin_menu(update, context, info=info)
    elif action == "stats":
        stats = service.get_statistics()
        info = (
            "📊 Статистика\n"
            f"• Пользователи: {stats['users']}\n"
            f"• Постов в очереди: {stats['posts_pending']}\n"
            f"• Опубликовано: {stats['posts_published']}\n"
            f"• Всего постов: {stats['posts_total']}"
        )
        await show_admin_menu(update, context, info=info)
    elif action == "toggle_pause":
        paused = service.is_autopost_paused()
        service.set_autopost_paused(not paused)
        info = (
            "▶️ Автопостинг возобновлён."
            if paused
            else "⏸ Автопостинг поставлен на паузу."
        )
        await show_admin_menu(update, context, info=info)
    elif action == "finance":
        summary = service.get_finance_summary()
        info = (
            "💰 Финансы\n"
            f"• Всего счетов: {summary['invoices_total']}\n"
            f"• Оплачено: {summary['invoices_paid']}\n"
            f"• Ожидает оплаты: {summary['invoices_pending']}\n"
            f"• Получено средств: {summary['revenue_collected']:.2f}\n"
            f"• В ожидании: {summary['revenue_waiting']:.2f}"
        )
        await show_admin_menu(update, context, info=info)
    elif action == "subscription":
        settings = service.get_settings()
        context.user_data.pop("awaiting_subscription_target", None)
        requirement = settings.subscription_chat_id
        link = settings.subscription_invite_link or _subscription_link(settings)
        lines = ["🔗 <b>Проверка подписки</b>"]
        if requirement:
            lines.append(
                f"• Проверяется подписка на: <code>{html.escape(str(requirement))}</code>"
            )
            if link:
                safe_link = html.escape(link, quote=True)
                lines.append(f"• Ссылка для пользователей: <a href=\"{safe_link}\">{safe_link}</a>")
            lines.append("")
            lines.append(
                "Пользователи должны быть подписаны на канал, чтобы пользоваться ботом."
            )
        else:
            lines.append("• Проверка подписки <b>отключена</b>.")
            lines.append("")
            lines.append("Укажите канал, чтобы активировать проверку.")
        lines.append("")
        lines.append(
            "Нажмите «Изменить ссылку», чтобы задать канал, или отключите проверку."
        )
        keyboard_rows = [
            [
                InlineKeyboardButton(
                    "✏️ Изменить ссылку", callback_data="admin:set_subscription"
                )
            ]
        ]
        if requirement:
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        "🚫 Отключить проверку", callback_data="admin:clear_subscription"
                    )
                ]
            )
        keyboard_rows.append(
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin:refresh")]
        )
        await query.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    elif action == "set_subscription":
        context.user_data["awaiting_subscription_target"] = True
        await query.message.edit_text(
            "Отправьте @username или числовой ID канала.\n"
            "Можно добавить ссылку после пробела, например: «@channel https://t.me/channel».\n"
            "Напишите «отмена», чтобы выйти без изменений.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="admin:subscription")]]
            ),
        )
    elif action == "clear_subscription":
        service.update_subscription_requirement(None, None)
        await show_admin_menu(update, context, info="✅ Проверка подписки отключена.")
    elif action == "requests":
        pending_posts = service.list_pending_posts()
        if not pending_posts:
            info = "🗂 Заявки на посты отсутствуют."
        else:
            preview_count = 5
            preview_lines = []
            for post in pending_posts[:preview_count]:
                snippet = post.text.strip().replace("\n", " ")
                if len(snippet) > 60:
                    snippet = snippet[:57] + "..."
                preview_lines.append(f"• #{post.post_id} от {post.user_id}: {snippet}")
            remaining = len(pending_posts) - preview_count
            if remaining > 0:
                preview_lines.append(f"… и ещё {remaining} в очереди.")
            info = "🗂 Заявки на посты:\n" + "\n".join(preview_lines)
        await show_admin_menu(update, context, info=info)
    elif action == "cryptopay":
        token_configured = bool(os.environ.get("CRYPTOPAY_TOKEN"))
        info = (
            "💳 CryptoPay\n"
            f"• Токен настроен: {'✅' if token_configured else '❌'}\n"
            f"• Последние счета создаются через API CryptoBot.\n"
            "• Используйте раздел «Проверить оплату», чтобы синхронизировать оплаты."
        )
        await show_admin_menu(update, context, info=info)
    elif action == "refresh":
        await show_admin_menu(update, context, info="🔄 Данные обновлены.")
    else:
        await query.answer("Неизвестный раздел", show_alert=True)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    if not await ensure_subscription(update, context):
        return
    await show_admin_menu(update, context)
