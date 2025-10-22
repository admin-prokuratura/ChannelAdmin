"""Executable Telegram bot wiring for the Channel Admin service."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta

from telegram import Update
from telegram.ext import AIORateLimiter, ApplicationBuilder, CommandHandler, ContextTypes

from .config import FilterConfig, PricingConfig
from .services import ChannelEconomyService
from .storage import InMemoryStorage

LOGGER = logging.getLogger(__name__)


def build_service() -> ChannelEconomyService:
    pricing = PricingConfig()
    filter_config = FilterConfig()
    storage = InMemoryStorage()
    return ChannelEconomyService(storage=storage, pricing=pricing, filter_config=filter_config)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service: ChannelEconomyService = context.application.bot_data.setdefault("service", build_service())
    try:
        service.register_user(update.effective_user.id, subscribed_to_sponsors=True)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    await update.message.reply_text("Вы успешно зарегистрированы и получили стартовую энергию!")


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service: ChannelEconomyService = context.application.bot_data.setdefault("service", build_service())
    user = service.get_user_balance(update.effective_user.id)
    if not user:
        await update.message.reply_text("Пользователь не найден. Используйте /start.")
        return
    await update.message.reply_text(
        f"Энергия: {user.energy}\nАктивных золотых карточек: {sum(1 for card in user.golden_cards if card.expires_at > card.purchased_at)}"
    )


async def buy_energy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service: ChannelEconomyService = context.application.bot_data.setdefault("service", build_service())
    if not context.args:
        await update.message.reply_text("Укажите количество энергии: /buy_energy 50")
        return
    amount = int(context.args[0])
    price = service.purchase_energy(update.effective_user.id, amount)
    await update.message.reply_text(f"Покупка успешна. Стоимость: {price:.2f}₽")


async def buy_golden_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service: ChannelEconomyService = context.application.bot_data.setdefault("service", build_service())
    if not context.args:
        await update.message.reply_text("Укажите длительность в часах: /buy_golden_card 24")
        return
    hours = int(context.args[0])
    price = service.purchase_golden_card(update.effective_user.id, timedelta(hours=hours))
    await update.message.reply_text(f"Золотая карточка приобретена. Стоимость: {price:.2f}₽")


async def post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service: ChannelEconomyService = context.application.bot_data.setdefault("service", build_service())
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


async def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required")
    application = (
        ApplicationBuilder()
        .token(token)
        .rate_limiter(AIORateLimiter())
        .post_init(lambda app: app.bot_data.setdefault("service", build_service()))
        .build()
    )

    application.add_handler(CommandHandler("start", start))
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
