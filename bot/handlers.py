from __future__ import annotations

import asyncio
import io
import tempfile
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .elevenlabs_client import ElevenLabsClient, ElevenLabsError, ElevenLabsVoiceFetchError
from .storage import VoiceEntry, VoiceStore


@dataclass(frozen=True)
class AppContext:
    store: VoiceStore
    elevenlabs: ElevenLabsClient


BTN_VOICES = "Голоса"
BTN_SYNC = "Синхронизировать"
BTN_MY_VOICE = "Текущий голос"
BTN_HELP = "Помощь"
BTN_ADD_WIZARD = "Создать голос (авто)"
BTN_CANCEL = "Отмена"
BTN_DONE = "Готово"
BTN_VISIBILITY_PRIVATE = "private"
BTN_VISIBILITY_PUBLIC = "public"
MAX_SAMPLE_FILES = 5


class AddVoiceWizard(StatesGroup):
    waiting_name = State()
    waiting_samples = State()
    waiting_visibility = State()


def build_router(context: AppContext) -> Router:
    router = Router()

    @router.message(Command("start"))
    async def start_handler(message: Message) -> None:
        text = (
            "Это voice-to-voice бот.\n\n"
            "1) Нажми 'Синхронизировать' (подтянутся cloned голоса из ElevenLabs)\n"
            "2) Открой 'Голоса' и выбери нужный\n"
            "3) Отправь voice/audio и получишь ответ другим голосом\n\n"
            "Новый голос проще создать через кнопку 'Создать голос (авто)'."
        )
        await message.answer(text, reply_markup=_main_menu_keyboard())

    @router.message(Command("help"))
    async def help_handler(message: Message) -> None:
        text = (
            "Команды:\n"
            "/voices - доступные голоса и выбор\n"
            "/syncvoices [all] - подтянуть голоса из ElevenLabs\n"
            "/myvoice - текущий голос\n"
            "/mode [natural|strong] - режим силы преобразования\n"
            "/voicemethod [sts|tts] - как обрабатывать голосовые\n"
            "/addwizard - создать голос из sample-аудио\n"
            "/createvoice - то же самое, алиас\n"
            "/addvoice <name> <voice_id> [public] - добавить/обновить голос\n"
            "/setvoice <name> - выбрать голос по имени\n\n"
            "/cancel - отменить текущий мастер\n\n"
            "Параметр public необязателен: public|yes|1\n"
            "Режим преобразования по умолчанию: strong.\n"
            "Метод голосовых по умолчанию: sts.\n"
            "Обычный текст бот озвучивает выбранным голосом.\n"
            "По умолчанию синхронизируются только твои cloned голоса.\n"
            "Для всех доступных голосов используй: /syncvoices all"
        )
        await message.answer(text, reply_markup=_main_menu_keyboard())

    @router.message(Command("menu"))
    async def menu_handler(message: Message) -> None:
        await message.answer("Главное меню открыто.", reply_markup=_main_menu_keyboard())

    @router.message(Command("cancel"))
    async def cancel_handler(message: Message, state: FSMContext) -> None:
        current_state = await state.get_state()
        if current_state is None:
            await message.answer("Нет активного действия.", reply_markup=_main_menu_keyboard())
            return
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=_main_menu_keyboard())

    @router.message(Command("addwizard"))
    @router.message(Command("createvoice"))
    @router.message(StateFilter(None), F.text == BTN_ADD_WIZARD)
    async def add_wizard_start_handler(message: Message, state: FSMContext) -> None:
        await state.set_state(AddVoiceWizard.waiting_name)
        await message.answer(
            "Шаг 1/3. Введи имя нового голоса (например: РЭД).",
            reply_markup=_wizard_cancel_keyboard(),
        )

    @router.message(AddVoiceWizard.waiting_name, F.text)
    async def add_wizard_name_handler(message: Message, state: FSMContext) -> None:
        name = message.text.strip()
        if not name or name == BTN_CANCEL:
            await message.answer("Имя пустое. Введи корректное имя или нажми /cancel.")
            return

        await state.update_data(name=name)
        await state.update_data(sample_files=[])
        await state.set_state(AddVoiceWizard.waiting_samples)
        await message.answer(
            f"Шаг 2/3. Отправь 1-{MAX_SAMPLE_FILES} голосовых sample (voice/audio).\n"
            "Когда закончишь, нажми 'Готово'.",
            reply_markup=_wizard_samples_keyboard(),
        )

    @router.message(AddVoiceWizard.waiting_samples, F.voice | F.audio)
    async def add_wizard_sample_handler(message: Message, state: FSMContext) -> None:
        sample = _extract_sample_meta(message)
        if sample is None:
            await message.answer("Не удалось прочитать sample. Отправь voice или audio.")
            return

        data = await state.get_data()
        sample_files = list(data.get("sample_files", []))
        if len(sample_files) >= MAX_SAMPLE_FILES:
            await message.answer(
                f"Максимум {MAX_SAMPLE_FILES} sample. Нажми 'Готово' для продолжения."
            )
            return
        sample_files.append(sample)
        await state.update_data(sample_files=sample_files)
        await message.answer(
            f"Sample добавлен ({len(sample_files)}/{MAX_SAMPLE_FILES}). "
            "Отправь еще или нажми 'Готово'."
        )

    @router.message(AddVoiceWizard.waiting_samples, F.text)
    async def add_wizard_samples_text_handler(message: Message, state: FSMContext) -> None:
        value = message.text.strip()
        if value == BTN_CANCEL:
            await state.clear()
            await message.answer("Добавление голоса отменено.", reply_markup=_main_menu_keyboard())
            return
        if value != BTN_DONE:
            await message.answer("Отправь voice/audio sample или нажми 'Готово'.")
            return

        data = await state.get_data()
        sample_files = list(data.get("sample_files", []))
        if len(sample_files) == 0:
            await message.answer("Добавь минимум один sample перед продолжением.")
            return

        await state.update_data(sample_files=sample_files)
        await state.set_state(AddVoiceWizard.waiting_visibility)
        await message.answer(
            "Шаг 3/3. Выбери видимость голоса.",
            reply_markup=_wizard_visibility_keyboard(),
        )

    @router.message(AddVoiceWizard.waiting_visibility, F.text)
    async def add_wizard_visibility_handler(message: Message, state: FSMContext) -> None:
        if message.text.strip() == BTN_CANCEL:
            await state.clear()
            await message.answer("Добавление голоса отменено.", reply_markup=_main_menu_keyboard())
            return

        visibility = _parse_visibility(message.text)
        if visibility is None:
            await message.answer(
                "Выбери один вариант: private или public. "
                "Для отмены нажми 'Отмена' или команду /cancel."
            )
            return

        data = await state.get_data()
        name = str(data.get("name", "")).strip()
        sample_files = list(data.get("sample_files", []))
        user_id = _extract_user_id(message)
        if user_id is None or not name or len(sample_files) == 0:
            await state.clear()
            await message.answer(
                "Не удалось завершить мастер. Запусти снова: /addwizard",
                reply_markup=_main_menu_keyboard(),
            )
            return

        status_message = await message.answer("Создаю голос в ElevenLabs...")
        try:
            prepared_samples = []
            for sample in sample_files:
                file_id = str(sample.get("file_id", "")).strip()
                filename = str(sample.get("filename", "sample")).strip()
                mime_type = str(sample.get("mime_type", "audio/mpeg")).strip()
                if not file_id:
                    continue
                content = await _download_tg_file_by_id(bot=message.bot, file_id=file_id)
                prepared_samples.append(
                    {"filename": filename, "content": content, "mime_type": mime_type}
                )
            created_voice = await context.elevenlabs.create_voice_from_samples(
                name=name,
                samples=prepared_samples,
            )
        except ElevenLabsError as error:
            await status_message.edit_text(f"Ошибка создания голоса: {error}")
            return
        except Exception as error:
            await status_message.edit_text(f"Не удалось создать голос: {error}")
            return

        voice = await context.store.add_voice(
            name=created_voice["name"],
            voice_id=created_voice["voice_id"],
            owner_telegram_id=user_id,
            is_public=visibility,
        )
        await context.store.set_selected_voice(user_id, voice.entry_id)
        await state.clear()
        await status_message.edit_text("Голос успешно создан в ElevenLabs.")
        await message.answer(
            f"Голос сохранен: {voice.name}\n"
            f"voice_id: {voice.voice_id}\n"
            f"visibility: {'public' if voice.is_public else 'private'}\n"
            "Он выбран как текущий.",
            reply_markup=_main_menu_keyboard(),
        )

    @router.message(StateFilter(None), F.text == BTN_VOICES)
    async def voices_button_handler(message: Message) -> None:
        await voices_handler(message)

    @router.message(StateFilter(None), F.text == BTN_SYNC)
    async def sync_button_handler(message: Message) -> None:
        await sync_voices_impl(message, only_cloned=True)

    @router.message(StateFilter(None), F.text == BTN_MY_VOICE)
    async def my_voice_button_handler(message: Message) -> None:
        await my_voice_handler(message)

    @router.message(StateFilter(None), F.text == BTN_HELP)
    async def help_button_handler(message: Message) -> None:
        await help_handler(message)

    @router.message(Command("addvoice"))
    async def add_voice_handler(message: Message, command: CommandObject) -> None:
        user_id = _extract_user_id(message)
        if user_id is None:
            await message.answer("Не удалось определить пользователя.")
            return

        if not command.args:
            await message.answer("Формат: /addvoice <name> <voice_id> [public]")
            return

        parts = command.args.split()
        if len(parts) < 2:
            await message.answer("Нужно минимум 2 аргумента: <name> <voice_id>")
            return

        name = parts[0].strip()
        voice_id = parts[1].strip()
        is_public = False
        if len(parts) >= 3:
            flag = parts[2].strip().lower()
            is_public = flag in {"public", "yes", "1", "true"}

        if not name or not voice_id:
            await message.answer("name и voice_id не должны быть пустыми.")
            return

        voice = await context.store.add_voice(
            name=name,
            voice_id=voice_id,
            owner_telegram_id=user_id,
            is_public=is_public,
        )
        await context.store.set_selected_voice(user_id, voice.entry_id)
        await message.answer(
            f"Голос сохранен: {voice.name}\n"
            f"voice_id: {voice.voice_id}\n"
            f"visibility: {'public' if voice.is_public else 'private'}\n"
            "Он выбран как текущий."
        )

    @router.message(Command("voices"))
    async def voices_handler(message: Message) -> None:
        user_id = _extract_user_id(message)
        if user_id is None:
            await message.answer("Не удалось определить пользователя.")
            return

        try:
            await _sync_from_elevenlabs(context, user_id, only_cloned=True)
        except ElevenLabsVoiceFetchError:
            pass

        voices = await context.store.list_available_voices(user_id)
        if not voices:
            await message.answer(
                "Пока нет голосов. Выполни /syncvoices, /addwizard или добавь вручную: "
                "/addvoice <name> <voice_id>"
            )
            return

        selected = await context.store.get_selected_voice(user_id)
        keyboard = _build_voices_keyboard(voices)
        lines = []
        for voice in voices:
            marker = " (current)" if selected and voice.entry_id == selected.entry_id else ""
            access = "public" if voice.is_public else "private"
            lines.append(f"- {voice.name} [{access}]{marker}")

        await message.answer("Доступные голоса:\n" + "\n".join(lines), reply_markup=keyboard)

    @router.message(Command("syncvoices"))
    async def sync_voices_handler(message: Message, command: CommandObject) -> None:
        only_cloned = True
        if command.args and command.args.strip().lower() == "all":
            only_cloned = False
        await sync_voices_impl(message, only_cloned=only_cloned)

    async def sync_voices_impl(message: Message, *, only_cloned: bool) -> None:
        user_id = _extract_user_id(message)
        if user_id is None:
            await message.answer("Не удалось определить пользователя.")
            return

        try:
            changed = await _sync_from_elevenlabs(context, user_id, only_cloned=only_cloned)
        except ElevenLabsVoiceFetchError as error:
            await message.answer(f"Ошибка синхронизации с ElevenLabs: {error}")
            return

        total = len(await context.store.list_available_voices(user_id))
        await message.answer(
            f"Синхронизация завершена.\n"
            f"Изменено/добавлено: {changed}\n"
            f"Всего доступно: {total}\n"
            "Открой /voices для выбора.",
            reply_markup=_main_menu_keyboard(),
        )

    @router.callback_query(F.data.startswith("pick_voice:"))
    async def pick_voice_callback(callback: CallbackQuery) -> None:
        user_id = _extract_callback_user_id(callback)
        if user_id is None:
            await callback.answer("Не удалось определить пользователя.", show_alert=True)
            return

        entry_id = callback.data.split(":", 1)[1]
        ok = await context.store.set_selected_voice(user_id, entry_id)
        if not ok:
            await callback.answer("Этот голос недоступен.", show_alert=True)
            return

        voice = await context.store.get_voice_by_entry_id(entry_id)
        if voice is None:
            await callback.answer("Голос не найден.", show_alert=True)
            return

        await callback.answer(f"Выбран голос: {voice.name}")
        if callback.message:
            await callback.message.answer(f"Текущий голос: {voice.name}")

    @router.message(Command("setvoice"))
    async def set_voice_handler(message: Message, command: CommandObject) -> None:
        user_id = _extract_user_id(message)
        if user_id is None:
            await message.answer("Не удалось определить пользователя.")
            return

        if not command.args:
            await message.answer("Формат: /setvoice <name>\nИли выбери через /voices")
            return

        matches = await context.store.find_available_by_name(user_id, command.args)
        if len(matches) == 0:
            await message.answer("Голос с таким именем не найден. Проверь /voices")
            return
        if len(matches) > 1:
            await message.answer("Несколько голосов с этим именем. Выбери через /voices")
            return

        chosen = matches[0]
        await context.store.set_selected_voice(user_id, chosen.entry_id)
        await message.answer(f"Выбран голос: {chosen.name}")

    @router.message(Command("myvoice"))
    async def my_voice_handler(message: Message) -> None:
        user_id = _extract_user_id(message)
        if user_id is None:
            await message.answer("Не удалось определить пользователя.")
            return

        selected = await context.store.get_selected_voice(user_id)
        if not selected:
            await message.answer("Текущий голос не выбран. Выбери через /voices")
            return

        mode = await context.store.get_conversion_mode(user_id)
        voice_method = await context.store.get_voice_method(user_id)
        await message.answer(
            "Текущий голос:\n"
            f"- name: {selected.name}\n"
            f"- voice_id: {selected.voice_id}\n"
            f"- mode: {mode}\n"
            f"- voice_method: {voice_method}"
        )

    @router.message(Command("mode"))
    async def mode_handler(message: Message, command: CommandObject) -> None:
        user_id = _extract_user_id(message)
        if user_id is None:
            await message.answer("Не удалось определить пользователя.")
            return

        if not command.args:
            current_mode = await context.store.get_conversion_mode(user_id)
            await message.answer(
                f"Текущий режим: {current_mode}\n"
                "Установить: /mode natural или /mode strong"
            )
            return

        requested_mode = command.args.strip().lower()
        ok = await context.store.set_conversion_mode(user_id, requested_mode)
        if not ok:
            await message.answer("Неверный режим. Используй: /mode natural или /mode strong")
            return

        await message.answer(f"Режим преобразования установлен: {requested_mode}")

    @router.message(Command("voicemethod"))
    async def voice_method_handler(message: Message, command: CommandObject) -> None:
        user_id = _extract_user_id(message)
        if user_id is None:
            await message.answer("Не удалось определить пользователя.")
            return

        if not command.args:
            current_method = await context.store.get_voice_method(user_id)
            await message.answer(
                f"Текущий метод голосовых: {current_method}\n"
                "Установить: /voicemethod sts или /voicemethod tts"
            )
            return

        requested = command.args.strip().lower()
        ok = await context.store.set_voice_method(user_id, requested)
        if not ok:
            await message.answer("Неверный метод. Используй: /voicemethod sts или /voicemethod tts")
            return
        await message.answer(f"Метод обработки голосовых установлен: {requested}")

    @router.message(F.voice | F.audio)
    async def process_audio_handler(message: Message, bot: Bot) -> None:
        user_id = _extract_user_id(message)
        if user_id is None:
            await message.answer("Не удалось определить пользователя.")
            return

        selected = await context.store.get_selected_voice(user_id)
        if not selected:
            await message.answer("Сначала выбери голос:\n1) /addwizard\n2) /voices")
            return

        conversion_mode = await context.store.get_conversion_mode(user_id)
        voice_method = await context.store.get_voice_method(user_id)
        status_message = await message.answer("Обрабатываю аудио...")
        try:
            audio_bytes, filename, mime_type = await _download_message_audio(message, bot)
            if voice_method == "tts":
                recognized = await context.elevenlabs.speech_to_text(
                    source_audio=audio_bytes,
                    source_filename=filename,
                    source_mime_type=mime_type,
                )
                converted = await context.elevenlabs.text_to_speech(
                    text=recognized,
                    target_voice_id=selected.voice_id,
                    conversion_mode=conversion_mode,
                )
            else:
                converted = await context.elevenlabs.speech_to_speech(
                    source_audio=audio_bytes,
                    source_filename=filename,
                    source_mime_type=mime_type,
                    target_voice_id=selected.voice_id,
                    conversion_mode=conversion_mode,
                )
        except ElevenLabsError as error:
            if voice_method == "tts":
                try:
                    converted = await context.elevenlabs.speech_to_speech(
                        source_audio=audio_bytes,
                        source_filename=filename,
                        source_mime_type=mime_type,
                        target_voice_id=selected.voice_id,
                        conversion_mode=conversion_mode,
                    )
                    await status_message.edit_text(
                        "STT/TTS не сработал, применил fallback STS. Отправляю результат..."
                    )
                except ElevenLabsError:
                    await status_message.edit_text(f"Ошибка ElevenLabs: {error}")
                    return
            else:
                await status_message.edit_text(f"Ошибка ElevenLabs: {error}")
                return
        except Exception as error:
            await status_message.edit_text(f"Ошибка обработки аудио: {error}")
            return

        try:
            converted_voice_ogg = await _convert_mp3_to_ogg_opus(converted)
            voice_output = BufferedInputFile(converted_voice_ogg, filename="converted.ogg")
            await message.answer_voice(
                voice=voice_output,
                caption=f"Голос: {selected.name} | mode: {conversion_mode} | method: {voice_method}",
            )
            await status_message.edit_text("Готово (отправлено как голосовое).")
        except Exception:
            output = BufferedInputFile(converted, filename="converted.mp3")
            await message.answer_audio(
                audio=output,
                title=f"Converted with {selected.name}",
                performer="VoiceBot",
                caption=f"Голос: {selected.name} | mode: {conversion_mode} | method: {voice_method}",
            )
            await status_message.edit_text("Готово (fallback: audio).")

    @router.message(StateFilter(None), F.text)
    async def process_text_handler(message: Message) -> None:
        text = (message.text or "").strip()
        if not text:
            return
        if text.startswith("/"):
            return
        if text in {
            BTN_VOICES,
            BTN_SYNC,
            BTN_MY_VOICE,
            BTN_HELP,
            BTN_ADD_WIZARD,
            BTN_CANCEL,
            BTN_DONE,
            BTN_VISIBILITY_PRIVATE,
            BTN_VISIBILITY_PUBLIC,
        }:
            return

        user_id = _extract_user_id(message)
        if user_id is None:
            await message.answer("Не удалось определить пользователя.")
            return

        selected = await context.store.get_selected_voice(user_id)
        if not selected:
            await message.answer("Сначала выбери голос:\n1) /addwizard\n2) /voices")
            return

        conversion_mode = await context.store.get_conversion_mode(user_id)
        status_message = await message.answer("Озвучиваю текст...")
        synthesized = b""
        try:
            synthesized = await context.elevenlabs.text_to_speech(
                text=text,
                target_voice_id=selected.voice_id,
                conversion_mode=conversion_mode,
            )
            converted_voice_ogg = await _convert_mp3_to_ogg_opus(synthesized)
            voice_output = BufferedInputFile(converted_voice_ogg, filename="tts.ogg")
            await message.answer_voice(
                voice=voice_output,
                caption=f"Голос: {selected.name} | mode: {conversion_mode}",
            )
            await status_message.edit_text("Готово (текст озвучен).")
        except ElevenLabsError as error:
            await status_message.edit_text(f"Ошибка ElevenLabs: {error}")
        except Exception:
            output = BufferedInputFile(synthesized, filename="tts.mp3")
            await message.answer_audio(
                audio=output,
                title=f"TTS with {selected.name}",
                performer="VoiceBot",
                caption=f"Голос: {selected.name} | mode: {conversion_mode}",
            )
            await status_message.edit_text("Готово (fallback: audio).")

    return router


def _build_voices_keyboard(voices: list[VoiceEntry]):
    builder = InlineKeyboardBuilder()
    for voice in voices:
        builder.button(text=f"Выбрать: {voice.name}", callback_data=f"pick_voice:{voice.entry_id}")
    builder.adjust(1)
    return builder.as_markup()


def _extract_user_id(message: Message | None) -> int | None:
    if message is None or message.from_user is None:
        return None
    return int(message.from_user.id)


def _extract_callback_user_id(callback: CallbackQuery) -> int | None:
    if callback.from_user is None:
        return None
    return int(callback.from_user.id)


def _main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_VOICES), KeyboardButton(text=BTN_SYNC)],
            [KeyboardButton(text=BTN_MY_VOICE), KeyboardButton(text=BTN_HELP)],
            [KeyboardButton(text=BTN_ADD_WIZARD)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _wizard_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def _wizard_visibility_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_VISIBILITY_PRIVATE), KeyboardButton(text=BTN_VISIBILITY_PUBLIC)],
            [KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _wizard_samples_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_DONE)], [KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def _parse_visibility(raw: str | None) -> bool | None:
    value = (raw or "").strip().lower()
    if value in {BTN_VISIBILITY_PRIVATE, "приватный"}:
        return False
    if value in {BTN_VISIBILITY_PUBLIC, "публичный"}:
        return True
    return None


def _extract_sample_meta(message: Message) -> dict[str, str] | None:
    if message.voice is not None:
        return {
            "file_id": message.voice.file_id,
            "filename": "sample.ogg",
            "mime_type": message.voice.mime_type or "audio/ogg",
        }
    if message.audio is not None:
        return {
            "file_id": message.audio.file_id,
            "filename": message.audio.file_name or "sample_audio",
            "mime_type": message.audio.mime_type or "audio/mpeg",
        }
    return None


async def _download_message_audio(message: Message, bot: Bot) -> tuple[bytes, str, str]:
    if message.voice is not None:
        file_id = message.voice.file_id
        filename = "input.ogg"
        mime_type = message.voice.mime_type or "audio/ogg"
    elif message.audio is not None:
        file_id = message.audio.file_id
        filename = message.audio.file_name or "input_audio"
        mime_type = message.audio.mime_type or "audio/mpeg"
    else:
        raise ValueError("Сообщение не содержит audio/voice.")

    tg_file = await bot.get_file(file_id)
    buffer = io.BytesIO()
    await bot.download(tg_file, destination=buffer)
    return buffer.getvalue(), filename, mime_type


async def _download_tg_file_by_id(bot: Bot, file_id: str) -> bytes:
    tg_file = await bot.get_file(file_id)
    buffer = io.BytesIO()
    await bot.download(tg_file, destination=buffer)
    return buffer.getvalue()


async def _convert_mp3_to_ogg_opus(source_mp3: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="voicebot-") as tmp_dir:
        in_path = Path(tmp_dir) / "input.mp3"
        out_path = Path(tmp_dir) / "output.ogg"
        in_path.write_bytes(source_mp3)

        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(in_path),
            "-c:a",
            "libopus",
            "-b:a",
            "48k",
            "-vbr",
            "on",
            str(out_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0 or not out_path.exists():
            details = stderr.decode("utf-8", errors="ignore")[:300]
            raise RuntimeError(f"ffmpeg conversion failed: {details}")

        return out_path.read_bytes()


async def _sync_from_elevenlabs(context: AppContext, user_id: int, *, only_cloned: bool) -> int:
    remote_voices = await context.elevenlabs.list_available_voices()
    if only_cloned:
        remote_voices = [
            voice
            for voice in remote_voices
            if str(voice.get("category", "")).lower() == "cloned" and not voice.get("is_shared", False)
        ]
    return await context.store.sync_user_voices_from_provider(
        owner_telegram_id=user_id,
        provider_voices=remote_voices,
    )
