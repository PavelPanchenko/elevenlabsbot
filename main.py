from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from bot.config import load_settings
from bot.elevenlabs_client import ElevenLabsClient
from bot.handlers import AppContext, build_router
from bot.storage import VoiceStore


async def run() -> None:
    settings = load_settings()

    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = Dispatcher()

    store = VoiceStore(settings.voices_file_path)
    await store.initialize()

    elevenlabs = ElevenLabsClient(
        api_key=settings.elevenlabs_api_key,
        sts_model_id=settings.elevenlabs_sts_model_id,
        tts_model_id=settings.elevenlabs_tts_model_id,
        output_format=settings.elevenlabs_output_format,
    )

    context = AppContext(store=store, elevenlabs=elevenlabs)
    dispatcher.include_router(build_router(context))

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Старт и главное меню"),
            BotCommand(command="menu", description="Открыть меню кнопок"),
            BotCommand(command="voices", description="Список и выбор голосов"),
            BotCommand(command="syncvoices", description="Синхронизировать голоса"),
            BotCommand(command="myvoice", description="Показать текущий голос"),
            BotCommand(command="mode", description="Режим преобразования natural/strong"),
            BotCommand(command="voicemethod", description="Метод voice: sts или tts"),
            BotCommand(command="addwizard", description="Создать голос из sample-аудио"),
            BotCommand(command="createvoice", description="Алиас: создать голос из sample-аудио"),
            BotCommand(command="addvoice", description="Добавить голос по voice_id"),
            BotCommand(command="cancel", description="Отменить текущее действие"),
            BotCommand(command="help", description="Помощь по использованию"),
        ]
    )

    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
