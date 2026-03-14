from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from .storage import VoiceStore


class AdminOnlyMiddleware(BaseMiddleware):
    def __init__(self, store: VoiceStore) -> None:
        self._store = store

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = _extract_event_user_id(event)
        if user_id is None:
            return await handler(event, data)

        is_allowed = await self._store.is_user_allowed(user_id)
        if is_allowed:
            return await handler(event, data)

        if isinstance(event, Message):
            await event.answer(
                "Доступ запрещен. Передай администратору твой Telegram ID:\n"
                f"`{user_id}`",
                parse_mode="Markdown",
            )
            return None
        if isinstance(event, CallbackQuery):
            await event.answer("Доступ запрещен.", show_alert=True)
            return None

        return None


def _extract_event_user_id(event: TelegramObject) -> int | None:
    from_user = getattr(event, "from_user", None)
    if from_user is None:
        return None
    return int(from_user.id)
