from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class AdminOnlyMiddleware(BaseMiddleware):
    def __init__(self, admin_user_id: int) -> None:
        self._admin_user_id = int(admin_user_id)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = _extract_event_user_id(event)
        if user_id is None or user_id == self._admin_user_id:
            return await handler(event, data)

        if isinstance(event, Message):
            await event.answer("Доступ запрещен. Обратись к администратору бота.")
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
