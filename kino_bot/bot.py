"""Обработчики команд и кнопок Telegram-бота (aiogram 3)."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .formatting import CAPTION_LIMIT, visible_len
import httpx

from .generator import (
    CandidatesExhaustedError,
    Draft,
    DraftStore,
    Generator,
    NoNewsError,
    NotEnoughForDigestError,
)
from .llm import LLMError
from .material import Material, build_material, looks_like_material

log = logging.getLogger(__name__)

STATUS_TEXT = "🔎 Ищу свежие новости и пишу пост…"
HELP_TEXT = (
    "Я помогаю вести канал про кино: нахожу свежие новости на киносайтах и в Telegram-каналах "
    "и пишу по ним авторский пост в стиле канала.\n\n"
    "Команды:\n"
    "/idea — пост по самому интересному свежему поводу\n"
    "/digest — подборка премьер\n"
    "/myid — показать ваш Telegram id\n"
    "/help — эта подсказка\n\n"
    "А ещё можно прислать мне материал, и я напишу пост по нему:\n"
    "• ссылку на статью, интервью или рецензию;\n"
    "• текст или свои заметки, например впечатления после показа или мероприятия, "
    "только с ними я могу писать от лица человека, который сам что-то видел;\n"
    "• пересланный пост из другого канала;\n"
    "• фото с подписью: фото станет картинкой поста.\n\n"
    "Под черновиком три кнопки:\n"
    "👍 <b>Супер</b> — опубликовать в канал\n"
    "🔄 <b>Придумай ещё</b> — пост по другой новости\n"
    "✖️ <b>Отмена</b> — удалить черновик"
)
NO_ACCESS = "Нет доступа. Узнать свой id для настройки доступа: /myid"


def draft_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍 Супер", callback_data=f"pub:{draft_id}"),
                InlineKeyboardButton(text="🔄 Придумай ещё", callback_data=f"more:{draft_id}"),
            ],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"cancel:{draft_id}")],
        ]
    )


async def send_post(bot: Bot, chat_id: int | str, text: str, photo: BufferedInputFile | str | None,
                    reply_markup: InlineKeyboardMarkup | None = None) -> tuple[list[Message], str | None]:
    """Отправляет пост так же, как он будет выглядеть в канале.

    До 1024 знаков с картинкой — одно сообщение (фото с подписью), длиннее — фото, затем текст.
    Возвращает отправленные сообщения и file_id фото (None, если фото не ушло).
    """
    sent: list[Message] = []
    if photo is not None:
        try:
            if visible_len(text) <= CAPTION_LIMIT:
                msg = await bot.send_photo(chat_id, photo, caption=text, reply_markup=reply_markup)
                return [msg], msg.photo[-1].file_id if msg.photo else None
            msg = await bot.send_photo(chat_id, photo)
            sent.append(msg)
            text_msg = await bot.send_message(chat_id, text, reply_markup=reply_markup)
            sent.append(text_msg)
            return sent, msg.photo[-1].file_id if msg.photo else None
        except TelegramBadRequest as exc:
            if sent:  # фото ушло, упал текст — это не проблема картинки
                raise
            log.warning("Telegram не принял картинку (%s), отправляю текстом", exc.message)
    msg = await bot.send_message(chat_id, text, reply_markup=reply_markup)
    return [msg], None


class KinoBot:
    def __init__(self, bot: Bot, generator: Generator, channel_id: int | str, admin_ids: set[int],
                 http: httpx.AsyncClient | None = None):
        self.bot = bot
        self.http = http
        self.generator = generator
        self.channel_id = channel_id
        self.admin_ids = admin_ids
        self.drafts = DraftStore()
        self._busy: set[int] = set()
        self._publishing: set[str] = set()
        self.router = Router()
        self._register()

    # --- доступ -------------------------------------------------------------
    def is_admin(self, user_id: int | None) -> bool:
        if not self.admin_ids:
            return True
        return user_id is not None and user_id in self.admin_ids

    # --- обработчики --------------------------------------------------------
    def _register(self) -> None:
        r = self.router
        r.message.register(self.cmd_start, CommandStart())
        r.message.register(self.cmd_start, Command("help"))
        r.message.register(self.cmd_myid, Command("myid"))
        r.message.register(self.cmd_idea, Command("idea"))
        r.message.register(self.cmd_digest, Command("digest"))
        r.message.register(self.any_text)
        r.callback_query.register(self.on_publish, F.data.startswith("pub:"))
        r.callback_query.register(self.on_more, F.data.startswith("more:"))
        r.callback_query.register(self.on_cancel, F.data.startswith("cancel:"))
        r.callback_query.register(self.on_unknown_callback)

    async def cmd_start(self, message: Message) -> None:
        if self.is_admin(message.from_user and message.from_user.id):
            await message.answer(HELP_TEXT)
        else:
            await message.answer(
                "Привет! Это служебный бот канала про кино. Работать с ним могут только администраторы.\n"
                "Ваш Telegram id можно узнать командой /myid."
            )

    async def cmd_myid(self, message: Message) -> None:
        user_id = message.from_user.id if message.from_user else message.chat.id
        await message.answer(f"Ваш Telegram id: <code>{user_id}</code>")

    async def cmd_idea(self, message: Message) -> None:
        if not self.is_admin(message.from_user and message.from_user.id):
            await message.answer(NO_ACCESS)
            return
        if message.chat.id in self._busy:
            await message.answer("Уже готовлю пост, подождите несколько секунд.")
            return
        await self.make_draft(message.chat.id)

    async def cmd_digest(self, message: Message) -> None:
        if not self.is_admin(message.from_user and message.from_user.id):
            await message.answer(NO_ACCESS)
            return
        if message.chat.id in self._busy:
            await message.answer("Уже готовлю пост, подождите.")
            return
        await self.make_draft(message.chat.id, mode="digest")

    async def any_text(self, message: Message) -> None:
        if not self.is_admin(message.from_user and message.from_user.id):
            await message.answer(NO_ACCESS)
            return
        # Фото из альбома без подписи — часть того же сообщения, отвечать на каждое не нужно
        if message.media_group_id and not message.caption:
            return
        if looks_like_material(message) and self.http is not None:
            if message.chat.id in self._busy:
                await message.answer("Уже готовлю пост, подождите и пришлите материал ещё раз.")
                return
            await self.make_draft(message.chat.id, mode="material", source_message=message)
            return
        await message.answer(
            "Чтобы получить пост, отправьте /idea или /digest, "
            "либо пришлите ссылку, текст или заметки (от 60 знаков).\nВсе команды: /help"
        )

    async def _check_callback(self, query: CallbackQuery) -> Draft | None:
        if not self.is_admin(query.from_user.id):
            await query.answer("Нет доступа", show_alert=True)
            return None
        if query.message and query.message.chat.id in self._busy:
            await query.answer("Подождите, готовлю пост…")
            return None
        draft_id = (query.data or "").split(":", 1)[-1]
        draft = self.drafts.get(draft_id)
        if draft is None or draft_id in self._publishing:
            await query.answer("Черновик уже неактуален", show_alert=True)
            if draft is None and query.message:
                await self._remove_keyboard(query.message.chat.id, query.message.message_id)
            return None
        return draft

    async def on_publish(self, query: CallbackQuery) -> None:
        draft = await self._check_callback(query)
        if draft is None:
            return
        self._publishing.add(draft.id)
        try:
            await query.answer("Публикую…")
            photo = draft.photo_file_id or (
                BufferedInputFile(draft.image.data, draft.image.filename) if draft.image else None
            )
            try:
                await send_post(self.bot, self.channel_id, draft.text, photo)
            except TelegramAPIError as exc:
                log.error("Ошибка публикации в канал %s: %s", self.channel_id, exc)
                await self.bot.send_message(
                    draft.chat_id,
                    f"❌ Не удалось опубликовать пост: {_describe_publish_error(exc)}\n\n"
                    "Черновик сохранён — после исправления нажмите «Супер» ещё раз.",
                )
                return
            self.drafts.pop(draft.id)
            log.info("Опубликован пост: %s (%s)", draft.topic, draft.item.link)
            if draft.keyboard_message_id:
                await self._remove_keyboard(draft.chat_id, draft.keyboard_message_id)
            await self.bot.send_message(draft.chat_id, "✅ Пост опубликован.\nЕщё идея — /idea")
        finally:
            self._publishing.discard(draft.id)

    async def on_more(self, query: CallbackQuery) -> None:
        draft = await self._check_callback(query)
        if draft is None:
            return
        await query.answer()
        self.drafts.pop(draft.id)
        if draft.keyboard_message_id:
            await self._remove_keyboard(draft.chat_id, draft.keyboard_message_id)
        if draft.mode == "material":
            # новый вариант по тому же материалу
            await self.make_draft(draft.chat_id, mode="material",
                                  material=Material(docs=draft.docs, image=draft.image, source_url=draft.source_url))
        else:
            await self.make_draft(draft.chat_id, mode=draft.mode)

    async def on_cancel(self, query: CallbackQuery) -> None:
        draft = await self._check_callback(query)
        if draft is None:
            return
        self.drafts.pop(draft.id)
        await query.answer("Отменено")
        for message_id in draft.message_ids:
            try:
                await self.bot.delete_message(draft.chat_id, message_id)
            except TelegramAPIError as exc:
                log.info("Не удалось удалить сообщение %s: %s", message_id, exc)
        await self.bot.send_message(draft.chat_id, "Отменено.\nНовая идея — /idea")

    async def on_unknown_callback(self, query: CallbackQuery) -> None:
        await query.answer("Черновик уже неактуален", show_alert=True)

    # --- генерация черновика ----------------------------------------------------
    async def make_draft(self, chat_id: int, mode: str = "auto", source_message: Message | None = None,
                         material: Material | None = None) -> None:
        if chat_id in self._busy:
            return
        self._busy.add(chat_id)
        status = None
        try:
            first = "📥 Разбираю материал…" if mode == "material" else STATUS_TEXT
            status = await self.bot.send_message(chat_id, first)
            last_text = first

            async def progress(text: str) -> None:
                nonlocal last_text
                if status is None or text == last_text:
                    return
                last_text = text
                try:
                    await self.bot.edit_message_text(text, chat_id=chat_id, message_id=status.message_id)
                except TelegramAPIError:
                    pass

            typing = asyncio.create_task(self._keep_typing(chat_id))
            try:
                if mode == "material":
                    if material is None:
                        material = await build_material(source_message, self.bot, self.http)
                    if not material.docs:
                        await self.bot.send_message(
                            chat_id,
                            "Не получилось открыть ссылку"
                            + (f" ({', '.join(material.failed_urls)})" if material.failed_urls else "")
                            + ". Пришлите текст статьи или свои заметки сообщением.",
                        )
                        return
                    if material.failed_urls:
                        await self.bot.send_message(
                            chat_id, "Не открылась ссылка: " + ", ".join(material.failed_urls)
                            + ". Пишу по остальному материалу.")
                    draft = await self.generator.generate_from_material(
                        chat_id, material.docs, image=material.image, source_url=material.source_url,
                        progress=progress)
                elif mode == "digest":
                    draft = await self.generator.generate_digest(chat_id, progress=progress)
                else:
                    draft = await self.generator.generate(chat_id, progress=progress)
            finally:
                typing.cancel()
            await self._send_draft(draft)
        except NoNewsError:
            await self.bot.send_message(
                chat_id, "Свежих новостей пока нет: источники пусты или недоступны. Попробуйте позже — /idea"
            )
        except NotEnoughForDigestError:
            await self.bot.send_message(
                chat_id, "Для подборки сейчас не набирается хотя бы трёх свежих премьер. Попробуйте позже "
                         "или возьмите один повод — /idea")
        except CandidatesExhaustedError:
            await self.bot.send_message(
                chat_id,
                "Свежие новости закончились — всё уже предлагалось. Список предложенных сброшен, "
                "можно начать заново: /idea",
            )
        except LLMError as exc:
            log.error("Ошибка генерации: %s", exc)
            await self.bot.send_message(chat_id, f"⚠️ Не получилось написать пост: {exc}.\nПопробуйте ещё раз — /idea")
        except Exception:  # noqa: BLE001 — любая ошибка не должна ронять бота
            log.exception("Непредвиденная ошибка генерации")
            await self.bot.send_message(chat_id, "⚠️ Что-то пошло не так при подготовке поста. Попробуйте ещё раз — /idea")
        finally:
            self._busy.discard(chat_id)
            if status is not None:
                try:
                    await status.delete()
                except TelegramAPIError:
                    pass

    async def _send_draft(self, draft: Draft) -> None:
        keyboard = draft_keyboard(draft.id)
        photo = BufferedInputFile(draft.image.data, draft.image.filename) if draft.image else None
        messages, file_id = await send_post(self.bot, draft.chat_id, draft.text, photo, keyboard)
        if photo is not None and file_id is None:
            draft.image = None  # Telegram не принял картинку — публикуем текстом, как в черновике
        draft.photo_file_id = file_id
        draft.message_ids = [m.message_id for m in messages]
        draft.keyboard_message_id = messages[-1].message_id
        self.drafts.add(draft)

    async def _keep_typing(self, chat_id: int) -> None:
        try:
            while True:
                await self.bot.send_chat_action(chat_id, "typing")
                await asyncio.sleep(4.5)
        except asyncio.CancelledError:
            pass
        except TelegramAPIError:
            pass

    async def _remove_keyboard(self, chat_id: int, message_id: int) -> None:
        try:
            await self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except TelegramAPIError:
            pass

    # --- ежедневная идея --------------------------------------------------------
    async def send_daily(self) -> None:
        if not self.admin_ids:
            log.warning("Ежедневная идея: ADMIN_IDS пуст, отправлять некому")
            return
        for admin_id in sorted(self.admin_ids):
            log.info("Ежедневная идея для %s", admin_id)
            try:
                await self.make_draft(admin_id)
            except TelegramAPIError as exc:  # например, администратор не начинал диалог с ботом
                log.error("Не удалось отправить ежедневную идею %s: %s", admin_id, exc)


def _describe_publish_error(exc: TelegramAPIError) -> str:
    text = str(getattr(exc, "message", "") or exc)
    low = text.lower()
    if "chat not found" in low:
        return "канал не найден — проверьте CHANNEL_ID"
    if "not enough rights" in low or "administrator" in low or "forbidden" in low or "not a member" in low:
        return "бот не администратор канала или у него нет права «Публикация сообщений»"
    return text
