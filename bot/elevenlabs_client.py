from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx


class ElevenLabsError(Exception):
    pass


class ElevenLabsVoiceFetchError(ElevenLabsError):
    pass


class ElevenLabsClient:
    def __init__(
        self,
        *,
        api_key: str,
        sts_model_id: str,
        tts_model_id: str,
        output_format: str,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._sts_model_id = sts_model_id
        self._tts_model_id = tts_model_id
        self._output_format = output_format
        self._timeout_seconds = timeout_seconds
        self._max_retries = 3

    async def speech_to_speech(
        self,
        *,
        source_audio: bytes,
        source_filename: str,
        source_mime_type: str,
        target_voice_id: str,
        conversion_mode: str = "strong",
    ) -> bytes:
        url = f"https://api.elevenlabs.io/v1/speech-to-speech/{target_voice_id}"
        headers = {
            "xi-api-key": self._api_key,
            "Accept": "audio/mpeg",
        }
        data: dict[str, str] = {
            "model_id": self._sts_model_id,
            "output_format": self._output_format,
        }
        data["voice_settings"] = json.dumps(_build_voice_settings(conversion_mode))
        files = {
            "audio": (source_filename, source_audio, source_mime_type),
        }

        response = await self._post_with_retries(url, headers=headers, data=data, files=files)

        if response.status_code >= 400:
            details = response.text[:500]
            raise ElevenLabsError(f"ElevenLabs вернул {response.status_code}: {details}")

        return response.content

    async def list_available_voices(self) -> list[dict[str, Any]]:
        url = "https://api.elevenlabs.io/v1/voices"
        headers = {
            "xi-api-key": self._api_key,
            "Accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.get(url, headers=headers)

        if response.status_code >= 400:
            details = response.text[:500]
            raise ElevenLabsVoiceFetchError(
                f"Не удалось получить список голосов ({response.status_code}): {details}"
            )

        payload = response.json()
        voices = payload.get("voices", [])
        if not isinstance(voices, list):
            return []

        result: list[dict[str, Any]] = []
        for voice in voices:
            if not isinstance(voice, dict):
                continue
            voice_id = str(voice.get("voice_id", "")).strip()
            name = str(voice.get("name", "")).strip()
            category = str(voice.get("category", "")).strip().lower()
            sharing = voice.get("sharing")
            is_shared = False
            if isinstance(sharing, dict):
                status = str(sharing.get("status", "")).strip().lower()
                is_shared = status in {"enabled", "public", "shared"}
            if not voice_id or not name:
                continue
            result.append(
                {
                    "voice_id": voice_id,
                    "name": name,
                    "category": category,
                    "is_shared": is_shared,
                }
            )
        return result

    async def create_voice_from_samples(
        self,
        *,
        name: str,
        samples: list[dict[str, bytes | str]],
    ) -> dict[str, str]:
        url = "https://api.elevenlabs.io/v1/voices/add"
        headers = {
            "xi-api-key": self._api_key,
            "Accept": "application/json",
        }
        data = {"name": name}
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for sample in samples:
            filename = str(sample["filename"])
            content = sample["content"]
            mime_type = str(sample["mime_type"])
            if not isinstance(content, (bytes, bytearray)):
                continue
            files.append(("files", (filename, bytes(content), mime_type)))

        if not files:
            raise ElevenLabsError("Нужно передать минимум один audio sample.")

        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(url, headers=headers, data=data, files=files)

        if response.status_code >= 400:
            details = response.text[:500]
            raise ElevenLabsError(f"Не удалось создать голос ({response.status_code}): {details}")

        payload = response.json()
        voice_id = str(payload.get("voice_id", "")).strip()
        resolved_name = str(payload.get("name", "")).strip() or name
        if not voice_id:
            raise ElevenLabsError("ElevenLabs не вернул voice_id для нового голоса.")

        return {"voice_id": voice_id, "name": resolved_name}

    async def text_to_speech(
        self,
        *,
        text: str,
        target_voice_id: str,
        conversion_mode: str = "strong",
    ) -> bytes:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{target_voice_id}"
        headers = {
            "xi-api-key": self._api_key,
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
        }
        payload = {
            "text": text,
            "model_id": self._tts_model_id,
            "output_format": self._output_format,
            "voice_settings": _build_voice_settings(conversion_mode),
        }

        response = await self._post_with_retries(url, headers=headers, json=payload)

        if response.status_code >= 400:
            details = response.text[:500]
            raise ElevenLabsError(f"TTS ошибка ({response.status_code}): {details}")

        return response.content

    async def speech_to_text(
        self,
        *,
        source_audio: bytes,
        source_filename: str,
        source_mime_type: str,
        model_id: str = "scribe_v1",
    ) -> str:
        url = "https://api.elevenlabs.io/v1/speech-to-text"
        headers = {
            "xi-api-key": self._api_key,
            "Accept": "application/json",
        }
        data = {"model_id": model_id}
        files = {
            "file": (source_filename, source_audio, source_mime_type),
        }
        response = await self._post_with_retries(url, headers=headers, data=data, files=files)
        if response.status_code >= 400:
            details = response.text[:500]
            raise ElevenLabsError(f"STT ошибка ({response.status_code}): {details}")

        payload = response.json()
        text = str(payload.get("text", "")).strip()
        if not text:
            raise ElevenLabsError("Распознавание вернуло пустой текст.")
        return text

    async def _post_with_retries(self, url: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                    response = await client.post(url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as error:
                last_error = error
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(1.2 * (attempt + 1))
                    continue
                raise ElevenLabsError(f"Сетевая ошибка ElevenLabs: {error}") from error

            if response.status_code >= 500:
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(1.2 * (attempt + 1))
                    continue
                raise ElevenLabsError(
                    "ElevenLabs временно недоступен (5xx). "
                    "Попробуй отправить сообщение еще раз через 10-20 секунд."
                )

            return response

        if last_error is not None:
            raise ElevenLabsError(f"Сетевая ошибка ElevenLabs: {last_error}") from last_error
        raise ElevenLabsError("Не удалось выполнить запрос к ElevenLabs.")


def _build_voice_settings(mode: str) -> dict[str, float | bool]:
    normalized = mode.strip().lower()
    if normalized == "natural":
        return {
            "stability": 0.55,
            "similarity_boost": 0.75,
            "style": 0.05,
            "use_speaker_boost": True,
        }
    return {
        "stability": 0.25,
        "similarity_boost": 0.95,
        "style": 0.2,
        "use_speaker_boost": True,
    }
