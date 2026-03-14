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
    admin_user_id: int
    store: VoiceStore
    elevenlabs: ElevenLabsClient


BTN_VOICES = "Голоса"
BTN_SYNC = "Синхронизировать"
BTN_MY_VOICE = "Текущий голос"
BTN_SETTINGS = "Настройки"
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


class AccessWizard(StatesGroup):
    waiting_allow_id = State()
    waiting_deny_id = State()


def build_router(context: AppContext) -> Router:
    router = Router()

    @router.message(Command("start"))
    async def start_handler(message: Message) -> None:
        text = (
            "Это voice-to-voice бот.\n\n"
            "1) Нажми 'Синхронизировать' (подтянутся cloned голоса из ElevenLabs)\n"
            "2) Открой 'Голоса' и выбери нужный\n"
            "3) Отправь voice/audio и получишь ответ другим голосом\n\n"
            "Новый голос проще создать через кнопку 'Создать голос (авто)'.\n"
            "Параметры работы меняются через кнопку 'Настройки'."
        )
        await message.answer(text, reply_markup=_main_menu_keyboard())

    @router.message(Command("help"))
    async def help_handler(message: Message) -> None:
        text = (
            "Команды:\n"
            "/voices - доступные голоса и выбор\n"
            "/syncvoices [all] - подтянуть голоса из ElevenLabs\n"
            "/settings - открыть настройки\n"
            "/myvoice - текущий голос\n"
            "/myid - показать мой Telegram ID\n"
            "/tokens (/balance) - остаток токенов\n"
            "/mode [natural|strong] - режим силы преобразования\n"
            "/voicemethod [sts|tts] - как обрабатывать голосовые\n"
            "/responsemode [auto|voice|audio] - формат ответа\n"
            "/addwizard - создать голос из sample-аудио\n"
            "/createvoice - то же самое, алиас\n"
            "/addvoice <name> <voice_id> [public] - добавить/обновить голос\n"
            "/setvoice <name> - выбрать голос по имени\n\n"
            "/allow <telegram_id> - выдать доступ (только админ)\n"
            "/deny <telegram_id> - забрать доступ (только админ)\n"
            "/allowed - список разрешенных ID (только админ)\n\n"
            "/cancel - отменить текущий мастер\n\n"
            "Параметр public необязателен: public|yes|1\n"
            "Режим преобразования по умолчанию: strong.\n"
            "Метод голосовых по умолчанию: sts.\n"
            "Формат ответа по умолчанию: auto.\n"
            "Обычный текст бот озвучивает выбранным голосом.\n"
            "По умолчанию синхронизируются только твои cloned голоса.\n"
            "Для всех доступных голосов используй: /syncvoices all"
        )
        await message.answer(text, reply_markup=_main_menu_keyboard())

    @router.message(Command("menu"))
    async def menu_handler(message: Message) -> None:
        await message.answer("Главное меню открыто.", reply_markup=_main_menu_keyboard())

    @router.message(Command("settings"))
    async def settings_handler(message: Message) -> None:
        user_id = _extract_user_id(message)
        await _show_settings(message, user_id=user_id)

    @router.message(Command("myid"))
    async def my_id_handler(message: Message) -> None:
        user_id = _extract_user_id(message)
        if user_id is None:
            await message.answer("Не удалось определить Telegram ID.")
            return
        await message.answer(f"Твой Telegram ID: `{user_id}`", parse_mode="Markdown")

    @router.message(Command("tokens"))
    @router.message(Command("balance"))
    async def tokens_handler(message: Message) -> None:
        line = await _build_tokens_line()
        await message.answer(line)

    @router.message(Command("allow"))
    async def allow_handler(message: Message, command: CommandObject) -> None:
        requester_id = _extract_user_id(message)
        if not _is_admin(requester_id, context):
            await message.answer("Команда доступна только администратору.")
            return
        target_id = _parse_user_id_arg(command.args)
        if target_id is None:
            await message.answer("Формат: /allow <telegram_id>")
            return
        changed = await context.store.allow_user(target_id)
        if changed:
            await message.answer(f"Доступ выдан пользователю: `{target_id}`", parse_mode="Markdown")
            return
        await message.answer(f"Пользователь уже в списке доступа: `{target_id}`", parse_mode="Markdown")

    @router.message(Command("deny"))
    async def deny_handler(message: Message, command: CommandObject) -> None:
        requester_id = _extract_user_id(message)
        if not _is_admin(requester_id, context):
            await message.answer("Команда доступна только администратору.")
            return
        target_id = _parse_user_id_arg(command.args)
        if target_id is None:
            await message.answer("Формат: /deny <telegram_id>")
            return
        if target_id == context.admin_user_id:
            await message.answer("Нельзя удалить доступ у админа.")
            return
        changed = await context.store.deny_user(target_id)
        if changed:
            await message.answer(f"Доступ удален у пользователя: `{target_id}`", parse_mode="Markdown")
            return
        await message.answer(f"Пользователь не найден в списке доступа: `{target_id}`", parse_mode="Markdown")

    @router.message(Command("allowed"))
    async def allowed_handler(message: Message) -> None:
        requester_id = _extract_user_id(message)
        if not _is_admin(requester_id, context):
            await message.answer("Команда доступна только администратору.")
            return
        allowed_ids = await context.store.list_allowed_users()
        if not allowed_ids:
            await message.answer("Список доступа пуст.")
            return
        lines = ["Разрешенные Telegram ID:"]
        lines.extend(f"- `{user_id}`" for user_id in allowed_ids)
        await message.answer("\n".join(lines), parse_mode="Markdown")

    @router.callback_query(F.data == "settings:access:list")
    async def settings_access_list_callback(callback: CallbackQuery) -> None:
        requester_id = _extract_callback_user_id(callback)
        if not _is_admin(requester_id, context):
            await callback.answer("Только для админа.", show_alert=True)
            return
        allowed_ids = await context.store.list_allowed_users()
        if not allowed_ids:
            await callback.message.answer("Список доступа пуст.")
        else:
            lines = ["Разрешенные Telegram ID:"]
            lines.extend(f"- `{user_id}`" for user_id in allowed_ids)
            await callback.message.answer("\n".join(lines), parse_mode="Markdown")
        await callback.answer()

    @router.callback_query(F.data == "settings:access:add")
    async def settings_access_add_callback(callback: CallbackQuery, state: FSMContext) -> None:
        requester_id = _extract_callback_user_id(callback)
        if not _is_admin(requester_id, context):
            await callback.answer("Только для админа.", show_alert=True)
            return
        await state.set_state(AccessWizard.waiting_allow_id)
        await callback.message.answer(
            "Введи Telegram ID пользователя для выдачи доступа.\n"
            "Отмена: /cancel"
        )
        await callback.answer()

    @router.callback_query(F.data == "settings:access:deny")
    async def settings_access_deny_callback(callback: CallbackQuery, state: FSMContext) -> None:
        requester_id = _extract_callback_user_id(callback)
        if not _is_admin(requester_id, context):
            await callback.answer("Только для админа.", show_alert=True)
            return
        await state.set_state(AccessWizard.waiting_deny_id)
        await callback.message.answer(
            "Введи Telegram ID пользователя для удаления доступа.\n"
            "Отмена: /cancel"
        )
        await callback.answer()

    @router.message(AccessWizard.waiting_allow_id, F.text)
    async def access_allow_id_input_handler(message: Message, state: FSMContext) -> None:
        requester_id = _extract_user_id(message)
        if not _is_admin(requester_id, context):
            await state.clear()
            await message.answer("Команда доступна только администратору.")
            return
        target_id = _parse_user_id_arg(message.text)
        if target_id is None:
            await message.answer("Неверный ID. Введи числовой Telegram ID или /cancel.")
            return
        changed = await context.store.allow_user(target_id)
        await state.clear()
        if changed:
            await message.answer(f"Доступ выдан: `{target_id}`", parse_mode="Markdown")
        else:
            await message.answer(f"ID уже был в доступе: `{target_id}`", parse_mode="Markdown")
        await _show_settings(message, user_id=requester_id)

    @router.message(AccessWizard.waiting_deny_id, F.text)
    async def access_deny_id_input_handler(message: Message, state: FSMContext) -> None:
        requester_id = _extract_user_id(message)
        if not _is_admin(requester_id, context):
            await state.clear()
            await message.answer("Команда доступна только администратору.")
            return
        target_id = _parse_user_id_arg(message.text)
        if target_id is None:
            await message.answer("Неверный ID. Введи числовой Telegram ID или /cancel.")
            return
        if target_id == context.admin_user_id:
            await message.answer("Нельзя удалить доступ у админа.")
            return
        changed = await context.store.deny_user(target_id)
        await state.clear()
        if changed:
            await message.answer(f"Доступ удален: `{target_id}`", parse_mode="Markdown")
        else:
            await message.answer(f"ID не найден в доступе: `{target_id}`", parse_mode="Markdown")
        await _show_settings(message, user_id=requester_id)

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
        user_id = _extract_user_id(message)
        if user_id is None:
            await message.answer("Не удалось определить пользователя.")
            return
        sync_scope = await context.store.get_sync_scope(user_id)
        await sync_voices_impl(message, only_cloned=_is_only_cloned_scope(sync_scope))

    @router.message(StateFilter(None), F.text == BTN_MY_VOICE)
    async def my_voice_button_handler(message: Message) -> None:
        await my_voice_handler(message)

    @router.message(StateFilter(None), F.text == BTN_SETTINGS)
    async def settings_button_handler(message: Message) -> None:
        user_id = _extract_user_id(message)
        await _show_settings(message, user_id=user_id)

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
            sync_scope = await context.store.get_sync_scope(user_id)
            await _sync_from_elevenlabs(
                context,
                user_id,
                only_cloned=_is_only_cloned_scope(sync_scope),
            )
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
        user_id = _extract_user_id(message)
        if user_id is None:
            await message.answer("Не удалось определить пользователя.")
            return
        only_cloned = _is_only_cloned_scope(await context.store.get_sync_scope(user_id))
        if command.args:
            arg = command.args.strip().lower()
            if arg in {"all", "cloned"}:
                await context.store.set_sync_scope(user_id, arg)
                only_cloned = _is_only_cloned_scope(arg)
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
        response_mode = await context.store.get_response_mode(user_id)
        await message.answer(
            "Текущий голос:\n"
            f"- name: {selected.name}\n"
            f"- voice_id: {selected.voice_id}\n"
            f"- mode: {mode}\n"
            f"- voice_method: {voice_method}\n"
            f"- response_mode: {response_mode}"
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

    @router.message(Command("responsemode"))
    async def response_mode_handler(message: Message, command: CommandObject) -> None:
        user_id = _extract_user_id(message)
        if user_id is None:
            await message.answer("Не удалось определить пользователя.")
            return

        if not command.args:
            current_mode = await context.store.get_response_mode(user_id)
            await message.answer(
                f"Текущий формат ответа: {current_mode}\n"
                "Установить: /responsemode auto | /responsemode voice | /responsemode audio"
            )
            return

        requested = command.args.strip().lower()
        ok = await context.store.set_response_mode(user_id, requested)
        if not ok:
            await message.answer(
                "Неверный формат. Используй: /responsemode auto | /responsemode voice | /responsemode audio"
            )
            return
        await message.answer(f"Формат ответа установлен: {requested}")

    @router.callback_query(F.data == "settings:open")
    async def settings_open_callback(callback: CallbackQuery) -> None:
        await _show_settings(callback.message, user_id=_extract_callback_user_id(callback))
        await callback.answer()

    @router.callback_query(F.data.startswith("settings:mode:"))
    async def settings_mode_callback(callback: CallbackQuery) -> None:
        user_id = _extract_callback_user_id(callback)
        if user_id is None or callback.data is None:
            await callback.answer("Не удалось обновить настройку.", show_alert=True)
            return
        mode = callback.data.split(":", 2)[2]
        ok = await context.store.set_conversion_mode(user_id, mode)
        if not ok:
            await callback.answer("Неверный режим.", show_alert=True)
            return
        await callback.answer(f"Режим: {mode}")
        await _show_settings(callback.message, user_id=user_id)

    @router.callback_query(F.data.startswith("settings:method:"))
    async def settings_method_callback(callback: CallbackQuery) -> None:
        user_id = _extract_callback_user_id(callback)
        if user_id is None or callback.data is None:
            await callback.answer("Не удалось обновить настройку.", show_alert=True)
            return
        method = callback.data.split(":", 2)[2]
        ok = await context.store.set_voice_method(user_id, method)
        if not ok:
            await callback.answer("Неверный метод.", show_alert=True)
            return
        await callback.answer(f"Метод: {method}")
        await _show_settings(callback.message, user_id=user_id)

    @router.callback_query(F.data.startswith("settings:sync:"))
    async def settings_sync_callback(callback: CallbackQuery) -> None:
        user_id = _extract_callback_user_id(callback)
        if user_id is None or callback.data is None:
            await callback.answer("Не удалось обновить настройку.", show_alert=True)
            return
        scope = callback.data.split(":", 2)[2]
        ok = await context.store.set_sync_scope(user_id, scope)
        if not ok:
            await callback.answer("Неверный режим синхронизации.", show_alert=True)
            return
        await callback.answer(f"Sync scope: {scope}")
        await _show_settings(callback.message, user_id=user_id)

    @router.callback_query(F.data.startswith("settings:response:"))
    async def settings_response_callback(callback: CallbackQuery) -> None:
        user_id = _extract_callback_user_id(callback)
        if user_id is None or callback.data is None:
            await callback.answer("Не удалось обновить настройку.", show_alert=True)
            return
        response_mode = callback.data.split(":", 2)[2]
        ok = await context.store.set_response_mode(user_id, response_mode)
        if not ok:
            await callback.answer("Неверный формат ответа.", show_alert=True)
            return
        await callback.answer(f"Формат ответа: {response_mode}")
        await _show_settings(callback.message, user_id=user_id)

    async def _show_settings(message: Message | None, *, user_id: int | None) -> None:
        if user_id is None or message is None:
            return
        mode = await context.store.get_conversion_mode(user_id)
        method = await context.store.get_voice_method(user_id)
        sync_scope = await context.store.get_sync_scope(user_id)
        response_mode = await context.store.get_response_mode(user_id)
        allowed_count = len(await context.store.list_allowed_users())
        tokens_line = await _build_tokens_line()
        text = (
            "Настройки бота:\n"
            f"- Режим преобразования: {mode}\n"
            f"- Метод голосовых: {method}\n"
            f"- Синхронизация голосов: {sync_scope}\n\n"
            f"- Формат ответа: {response_mode}\n\n"
            f"- Пользователей с доступом: {allowed_count}\n\n"
            f"- {tokens_line}\n\n"
            "Выбери параметры кнопками ниже."
        )
        await message.answer(
            text,
            reply_markup=_build_settings_keyboard(
                mode,
                method,
                sync_scope,
                response_mode,
                is_admin=_is_admin(user_id, context),
            ),
        )

    async def _build_tokens_line() -> str:
        try:
            info = await context.elevenlabs.get_tokens_info()
        except ElevenLabsError as error:
            details = str(error)
            lowered = details.lower()
            if "missing_permissions" in lowered:
                return "Токены: нет прав у API ключа (нужен scope на subscription/balance)."
            if len(details) > 120:
                details = details[:117] + "..."
            return f"Токены: недоступно ({details})"

        tier = info.get("tier", "unknown")
        tokens_left = info.get("tokens_left")
        used = info.get("character_count")
        limit = info.get("character_limit")

        if isinstance(tokens_left, int):
            return f"Токены: {tokens_left} (tier: {tier})"
        if isinstance(used, int) and isinstance(limit, int):
            remaining = max(limit - used, 0)
            return f"Токены: {remaining} из {limit} (tier: {tier})"
        return f"Токены: неизвестно (tier: {tier})"

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
        response_mode = await context.store.get_response_mode(user_id)
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
            await _send_mp3_result(
                message,
                status_message=status_message,
                source_mp3=converted,
                caption=f"Голос: {selected.name} | mode: {conversion_mode} | method: {voice_method}",
                audio_title=f"Converted with {selected.name}",
                response_mode=response_mode,
            )
        except Exception as error:
            await status_message.edit_text(f"Не удалось отправить результат: {error}")

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
            BTN_SETTINGS,
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
        response_mode = await context.store.get_response_mode(user_id)
        status_message = await message.answer("Озвучиваю текст...")
        synthesized = b""
        try:
            synthesized = await context.elevenlabs.text_to_speech(
                text=text,
                target_voice_id=selected.voice_id,
                conversion_mode=conversion_mode,
            )
            await _send_mp3_result(
                message,
                status_message=status_message,
                source_mp3=synthesized,
                caption=f"Голос: {selected.name} | mode: {conversion_mode}",
                audio_title=f"TTS with {selected.name}",
                response_mode=response_mode,
            )
        except ElevenLabsError as error:
            await status_message.edit_text(f"Ошибка ElevenLabs: {error}")
        except Exception as error:
            await status_message.edit_text(f"Ошибка обработки текста: {error}")

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


def _is_admin(user_id: int | None, context: AppContext) -> bool:
    return user_id is not None and int(user_id) == int(context.admin_user_id)


def _parse_user_id_arg(raw_args: str | None) -> int | None:
    if not raw_args:
        return None
    value = raw_args.strip().split()[0]
    if value.startswith("@"):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_VOICES), KeyboardButton(text=BTN_SYNC)],
            [KeyboardButton(text=BTN_MY_VOICE), KeyboardButton(text=BTN_SETTINGS)],
            [KeyboardButton(text=BTN_HELP)],
            [KeyboardButton(text=BTN_ADD_WIZARD)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _build_settings_keyboard(
    mode: str,
    method: str,
    sync_scope: str,
    response_mode: str,
    *,
    is_admin: bool,
):
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{'✅' if mode == 'natural' else '▫️'} Mode natural",
        callback_data="settings:mode:natural",
    )
    builder.button(
        text=f"{'✅' if mode == 'strong' else '▫️'} Mode strong",
        callback_data="settings:mode:strong",
    )
    builder.button(
        text=f"{'✅' if method == 'sts' else '▫️'} Voice method STS",
        callback_data="settings:method:sts",
    )
    builder.button(
        text=f"{'✅' if method == 'tts' else '▫️'} Voice method TTS",
        callback_data="settings:method:tts",
    )
    builder.button(
        text=f"{'✅' if sync_scope == 'cloned' else '▫️'} Sync cloned",
        callback_data="settings:sync:cloned",
    )
    builder.button(
        text=f"{'✅' if sync_scope == 'all' else '▫️'} Sync all",
        callback_data="settings:sync:all",
    )
    builder.button(
        text=f"{'✅' if response_mode == 'auto' else '▫️'} Response auto",
        callback_data="settings:response:auto",
    )
    builder.button(
        text=f"{'✅' if response_mode == 'voice' else '▫️'} Response voice",
        callback_data="settings:response:voice",
    )
    builder.button(
        text=f"{'✅' if response_mode == 'audio' else '▫️'} Response audio",
        callback_data="settings:response:audio",
    )
    if is_admin:
        builder.button(text="Доступ: список", callback_data="settings:access:list")
        builder.button(text="Доступ: добавить", callback_data="settings:access:add")
        builder.button(text="Доступ: удалить", callback_data="settings:access:deny")
        builder.adjust(2, 2, 2, 2, 1, 2, 1)
    else:
        builder.adjust(2, 2, 2, 2, 1)
    return builder.as_markup()


def _is_only_cloned_scope(scope: str) -> bool:
    return scope.strip().lower() != "all"


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


async def _send_mp3_result(
    message: Message,
    *,
    status_message: Message,
    source_mp3: bytes,
    caption: str,
    audio_title: str,
    response_mode: str,
) -> None:
    if response_mode == "audio":
        output = BufferedInputFile(source_mp3, filename="result.mp3")
        await message.answer_audio(
            audio=output,
            title=audio_title,
            performer="VoiceBot",
            caption=caption,
        )
        await status_message.edit_text("Готово (режим audio).")
        return

    if response_mode == "voice":
        converted_voice_ogg = await _convert_mp3_to_ogg_opus(source_mp3)
        voice_output = BufferedInputFile(converted_voice_ogg, filename="result.ogg")
        await message.answer_voice(voice=voice_output, caption=caption)
        await status_message.edit_text("Готово (режим voice).")
        return

    try:
        converted_voice_ogg = await _convert_mp3_to_ogg_opus(source_mp3)
        voice_output = BufferedInputFile(converted_voice_ogg, filename="result.ogg")
        await message.answer_voice(voice=voice_output, caption=caption)
        await status_message.edit_text("Готово (авто: голосовое).")
    except Exception:
        output = BufferedInputFile(source_mp3, filename="result.mp3")
        await message.answer_audio(
            audio=output,
            title=audio_title,
            performer="VoiceBot",
            caption=caption,
        )
        await status_message.edit_text("Готово (авто: fallback audio).")


async def _sync_from_elevenlabs(context: AppContext, user_id: int, *, only_cloned: bool) -> int:
    remote_all = await context.elevenlabs.list_available_voices()
    remote_voices = remote_all
    if only_cloned:
        remote_voices = [
            voice
            for voice in remote_all
            if str(voice.get("category", "")).lower() == "cloned" and not voice.get("is_shared", False)
        ]
    all_provider_ids = [str(item.get("voice_id", "")).strip() for item in remote_all]
    return await context.store.sync_user_voices_from_provider(
        owner_telegram_id=user_id,
        provider_voices=remote_voices,
        provider_voice_ids_for_prune=all_provider_ids,
    )
