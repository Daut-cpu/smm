"""Точка входа: python -m kino_bot"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from .bot import KinoBot
from .config import Config, ConfigError
from .generator import Generator
from .images import ImageFinder
from .llm import PostWriter
from .news import NewsCollector
from .scheduler import daily_loop, refresh_loop
from .sources.rss import make_http_client
from .sources.telegram import TelegramReader
from .style import StyleProvider

log = logging.getLogger("kino_bot")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        log.error("Ошибка настроек: %s", exc)
        sys.exit(2)

    if not config.admin_ids:
        log.warning("ADMIN_IDS пуст — бот доступен ВСЕМ пользователям Telegram. Задайте ADMIN_IDS в .env!")

    http = make_http_client()

    tg_reader = None
    if config.telegram_reader_enabled:
        tg_reader = TelegramReader(
            api_id=config.tg_api_id,
            api_hash=config.tg_api_hash,
            session=config.tg_session,
            channels=config.tg_channels,
            max_age_hours=config.max_age_hours,
        )
        try:
            await tg_reader.start()
        except Exception as exc:  # noqa: BLE001
            log.error("Не удалось подключить аккаунт для чтения каналов: %s", exc)
    else:
        log.warning("Чтение Telegram-каналов выключено: не заданы TG_API_ID, TG_API_HASH или "
                    "TG_CHANNELS/STYLE_CHANNEL")

    collector = NewsCollector(config.feeds, http, tg_reader, config.max_age_hours)
    writer = PostWriter(config.anthropic_api_key, config.model, config.effort, config.channel_style,
                        config.post_max_len)
    style = StyleProvider(tg_reader, config.style_channel, config.style_examples, config.style_file)
    generator = Generator(collector, writer, ImageFinder(http, tg_reader), config.show_source,
                          style=style, max_len=config.post_max_len)

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True))
    app = KinoBot(bot, generator, config.channel_id, config.admin_ids)
    dp = Dispatcher()
    dp.include_router(app.router)

    await bot.set_my_commands([
        BotCommand(command="idea", description="Идея поста по свежей новости"),
        BotCommand(command="help", description="Что умеет бот"),
        BotCommand(command="myid", description="Мой Telegram id"),
    ])

    tasks = [asyncio.create_task(refresh_loop(collector))]
    if config.daily_time:
        tasks.append(asyncio.create_task(daily_loop(config.daily_time, config.tz, app.send_daily)))
    else:
        log.info("Ежедневная идея выключена (DAILY_TIME не задан)")

    log.info("Бот запущен. Канал: %s, модель: %s, сайтов: %d, Telegram-каналов: %d",
             config.channel_id, config.model, len(config.feeds), len(config.tg_channels))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in tasks:
            task.cancel()
        await http.aclose()
        if tg_reader:
            await tg_reader.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
