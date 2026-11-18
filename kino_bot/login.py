"""Однократный вход аккаунта для чтения Telegram-каналов: python -m kino_bot.login

Telegram пришлёт код в приложение этого аккаунта; при включённой двухэтапной
аутентификации понадобится пароль. Сессия сохраняется в файл TG_SESSION.session,
после чего бот читает каналы без повторного входа.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv


async def main() -> None:
    from telethon import TelegramClient

    load_dotenv(".env")
    api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    session = os.environ.get("TG_SESSION", "").strip() or "data/reader"
    if not api_id or not api_hash:
        raise SystemExit("Задайте TG_API_ID и TG_API_HASH в .env (получить на https://my.telegram.org)")
    Path(session).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(session, int(api_id), api_hash)
    await client.start()  # спросит телефон, код и пароль 2FA
    me = await client.get_me()
    print(f"Готово: вход выполнен как {me.first_name or ''} (id {me.id}). Сессия: {session}.session")
    from .config import _normalize_channel, _split

    channels = [_normalize_channel(c) for c in _split(os.environ.get("TG_CHANNELS"))]
    for channel in channels:
        try:
            entity = await client.get_entity(channel)
            print(f"  ✓ @{channel}: {getattr(entity, 'title', '')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ @{channel}: {exc}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
