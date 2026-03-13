from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_admin_id: int
    elevenlabs_api_key: str
    elevenlabs_sts_model_id: str
    elevenlabs_tts_model_id: str
    elevenlabs_output_format: str
    voices_file_path: Path


def load_settings() -> Settings:
    load_dotenv()

    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_admin_id_raw = os.getenv("TELEGRAM_ADMIN_ID", "").strip()
    elevenlabs_api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    legacy_model_id = os.getenv("ELEVENLABS_MODEL_ID", "").strip()
    elevenlabs_sts_model_id = os.getenv("ELEVENLABS_STS_MODEL_ID", legacy_model_id).strip()
    if not elevenlabs_sts_model_id:
        elevenlabs_sts_model_id = "eleven_multilingual_sts_v2"

    elevenlabs_tts_model_id = os.getenv("ELEVENLABS_TTS_MODEL_ID", "").strip()
    if not elevenlabs_tts_model_id:
        elevenlabs_tts_model_id = "eleven_multilingual_v2"

    elevenlabs_output_format = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128").strip()
    data_dir = os.getenv("BOT_DATA_DIR", "data").strip()

    if not telegram_bot_token:
        raise ValueError("Не задан TELEGRAM_BOT_TOKEN")
    if not telegram_admin_id_raw:
        raise ValueError("Не задан TELEGRAM_ADMIN_ID")
    if not elevenlabs_api_key:
        raise ValueError("Не задан ELEVENLABS_API_KEY")
    try:
        telegram_admin_id = int(telegram_admin_id_raw)
    except ValueError as error:
        raise ValueError("TELEGRAM_ADMIN_ID должен быть числом") from error

    data_path = Path(data_dir)
    data_path.mkdir(parents=True, exist_ok=True)

    return Settings(
        telegram_bot_token=telegram_bot_token,
        telegram_admin_id=telegram_admin_id,
        elevenlabs_api_key=elevenlabs_api_key,
        elevenlabs_sts_model_id=elevenlabs_sts_model_id,
        elevenlabs_tts_model_id=elevenlabs_tts_model_id,
        elevenlabs_output_format=elevenlabs_output_format,
        voices_file_path=data_path / "voices.json",
    )
