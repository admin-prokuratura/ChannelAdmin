"""Executable Telegram bot wiring for the Channel Admin service."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    AIORateLimiter,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import FilterConfig, PricingConfig
from .services import ChannelEconomyService
from .storage import InMemoryStorage
from .payments import CryptoPayClient, CryptoPayError

LOGGER = logging.getLogger(__name__)


def build_service() -> ChannelEconomyService:
    pricing = PricingConfig()
    filter_config = FilterConfig()
    storage = InMemoryStorage()
    return ChannelEconomyService(storage=storage, pricing=pricing, filter_config=filter_config)


def build_crypto_client() -> CryptoPayClient | None:
    token = os.environ.get("CRYPTOPAY_TOKEN")
    if not token:
        LOGGER.warning("CRYPTOPAY_TOKEN is not configured; payments will be disabled")
        return None
    return CryptoPayClient(token=token)


def ensure_dependencies(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.application.bot_data.setdefault("service", build_service())
    context.application.bot_data.setdefault("crypto", build_crypto_client())


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Баланс", callback_data="action:balance"),
                InlineKeyboardButton("⚡️ Пополнить энергию", callback_data="action:energy"),
            ],
            [
                InlineKeyboardButton(
                    "🌟 Золотая карточка", callback_data="action:golden_card"
                ),
            ],
            [
                InlineKeyboardButton("📝 Отправить пост", callback_data="action:post"),
            ],
        ]
    )


def energy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("50 ⚡️", callback_data="energy:50"),
                InlineKeyboardButton("100 ⚡️", callback_data="energy:100"),
            ],
            [
                InlineKeyboardButton("250 ⚡️", callback_data="energy:250"),
            ],
            [InlineKeyboardButton("🔙 В меню", callback_data="action:menu")],
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
            [InlineKeyboardButton("🔙 В меню", callback_data="action:menu")],
        ]
    )


async def send_main_menu(update: Update, text: str) -> None:
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard())
    elif update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=main_menu_keyboard())


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
    try:
        service.register_user(update.effective_user.id, subscribed_to_sponsors=True)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    ensure_dependencies(context)
    await update.message.reply_text(
        "👋 Добро пожаловать в Channel Admin!\n"
        "Вы успешно зарегистрированы и получили стартовую энергию."
    )
    await send_main_menu(update, "Выберите действие из меню 👇")


async def handle_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    query = update.callback_query
    assert query is not None
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "menu":
        context.user_data.pop("awaiting_post", None)
        await query.message.edit_text("Выберите действие из меню 👇", reply_markup=main_menu_keyboard())
        return

    service = get_service(context)
    if action == "balance":
        user = service.get_user_balance(update.effective_user.id)
        if not user:
            await query.message.edit_text(
                "❗️ Пользователь не найден. Нажмите /start для регистрации.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 В меню", callback_data="action:menu")]]
                ),
            )
            return
        active_cards = sum(1 for card in user.golden_cards if card.expires_at > card.purchased_at)
        await query.message.edit_text(
            "📊 Ваш баланс:\n"
            f"• ⚡️ Энергия: {user.energy}\n"
            f"• 🌟 Активные золотые карточки: {active_cards}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 В меню", callback_data="action:menu")]]
            ),
        )
    elif action == "energy":
        await query.message.edit_text(
            "⚡️ Выберите пакет энергии для пополнения:",
            reply_markup=energy_keyboard(),
        )
    elif action == "golden_card":
        await query.message.edit_text(
            "🌟 Выберите длительность золотой карточки:",
            reply_markup=golden_card_keyboard(),
        )
    elif action == "post":
        context.user_data["awaiting_post"] = True
        await query.message.edit_text(
            "📝 Отправьте текст поста одним сообщением.\n"
            "Когда будете готовы, просто отправьте его в чат."
            "\n\nДля отмены нажмите кнопку ниже.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Отмена", callback_data="action:menu")]]
            ),
        )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service = get_service(context)
    user = service.get_user_balance(update.effective_user.id)
    if not user:
        await update.message.reply_text("Пользователь не найден. Используйте /start.")
        return
    await update.message.reply_text(
        f"Энергия: {user.energy}\nАктивных золотых карточек: {sum(1 for card in user.golden_cards if card.expires_at > card.purchased_at)}"
    )


async def buy_energy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service = get_service(context)
    if not context.args:
        await update.message.reply_text("Укажите количество энергии: /buy_energy 50")
        return
    amount = int(context.args[0])
    price = service.purchase_energy(update.effective_user.id, amount)
    await update.message.reply_text(f"Покупка успешна. Стоимость: {price:.2f}₽")


async def buy_golden_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service = get_service(context)
    if not context.args:
        await update.message.reply_text("Укажите длительность в часах: /buy_golden_card 24")
        return
    hours = int(context.args[0])
    price = service.purchase_golden_card(update.effective_user.id, timedelta(hours=hours))
    await update.message.reply_text(f"Золотая карточка приобретена. Стоимость: {price:.2f}₽")


async def post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service = get_service(context)
    if not context.args:
        await update.message.reply_text("Укажите текст поста после команды /post")
        return
    message = " ".join(context.args)
    try:
        new_post = service.submit_post(update.effective_user.id, message)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    pin_text = " Пост будет закреплён." if new_post.requires_pin else ""
    await update.message.reply_text(f"Пост одобрен и будет отправлен в канал.{pin_text}")


async def handle_energy_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    query = update.callback_query
    assert query is not None
    await query.answer()
    try:
        amount = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный выбор", show_alert=True)
        return

    service = get_service(context)
    price = service.pricing.price_for_energy(amount)
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

    await query.message.reply_text(
        "💳 Счёт для пополнения готов!\n"
        f"Сумма к оплате: {price:.2f} ₽\n"
        f"Перейдите по ссылке и завершите оплату: {invoice.pay_url}"
    )


async def handle_golden_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_dependencies(context)
    query = update.callback_query
    assert query is not None
    await query.answer()
    try:
        hours = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный выбор", show_alert=True)
        return

    service = get_service(context)
    duration = timedelta(hours=hours)
    price = service.pricing.price_for_golden_card(duration)
    client = get_crypto_client(context)
    if client is None:
        await query.message.reply_text(
            "💤 Платёжный шлюз пока не настроен. Обратитесь к администратору."
        )
        return

    try:
        invoice = await client.create_invoice(
            amount=price,
            description=f"Золотая карточка на {hours}ч",
            payload=f"golden:{update.effective_user.id}:{hours}",
        )
    except CryptoPayError as exc:
        await query.message.reply_text(f"❌ Не удалось создать счёт: {exc}")
        return

    await query.message.reply_text(
        "🌟 Счёт на золотую карточку готов!\n"
        f"Стоимость: {price:.2f} ₽\n"
        f"Оплатите по ссылке: {invoice.pay_url}"
    )


async def handle_post_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_post"):
        await update.message.reply_text(
            "🙌 Используйте кнопки ниже для управления ботом.",
            reply_markup=main_menu_keyboard(),
        )
        return

    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Сообщение не должно быть пустым. Попробуйте ещё раз ✍️")
        return

    service = get_service(context)
    try:
        new_post = service.submit_post(update.effective_user.id, text)
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return

    context.user_data.pop("awaiting_post", None)
    pin_text = " 📌 Пост будет закреплён." if new_post.requires_pin else ""
    await update.message.reply_text(
        "✅ Пост принят и отправлен на модерацию!" + pin_text
    )
    await update.message.reply_text(
        "Выберите следующее действие 👇",
        reply_markup=main_menu_keyboard(),
    )


async def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required")
    application = (
        ApplicationBuilder()
        .token(token)
        .rate_limiter(AIORateLimiter())
        .post_init(
            lambda app: (
                app.bot_data.setdefault("service", build_service()),
                app.bot_data.setdefault("crypto", build_crypto_client()),
            )
        )
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_menu_action, pattern="^action:"))
    application.add_handler(CallbackQueryHandler(handle_energy_selection, pattern="^energy:"))
    application.add_handler(CallbackQueryHandler(handle_golden_selection, pattern="^golden:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_post_text))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("buy_energy", buy_energy))
    application.add_handler(CommandHandler("buy_golden_card", buy_golden_card))
    application.add_handler(CommandHandler("post", post))

    LOGGER.info("Bot started")
    await application.initialize()
    await application.start()
    try:
        await application.updater.start_polling()
        await application.updater.wait_until_closed()
    finally:
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
