from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class VoiceEntry:
    entry_id: str
    name: str
    voice_id: str
    owner_telegram_id: int
    is_public: bool
    created_at: str


class VoiceStore:
    def __init__(self, file_path: Path) -> None:
        self._file_path = file_path
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if self._file_path.exists():
                return
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_data(
                {
                    "voices": [],
                    "selected_voice_by_user": {},
                    "conversion_mode_by_user": {},
                    "voice_method_by_user": {},
                    "sync_scope_by_user": {},
                    "response_mode_by_user": {},
                    "allowed_user_ids": [],
                }
            )

    async def add_voice(
        self,
        *,
        name: str,
        voice_id: str,
        owner_telegram_id: int,
        is_public: bool,
    ) -> VoiceEntry:
        async with self._lock:
            data = self._read_data()
            existing = self._find_existing_voice(data["voices"], voice_id, owner_telegram_id)
            if existing is not None:
                existing["name"] = name
                existing["is_public"] = is_public
                self._write_data(data)
                return self._to_voice_entry(existing)

            entry = {
                "entry_id": str(uuid4()),
                "name": name,
                "voice_id": voice_id,
                "owner_telegram_id": owner_telegram_id,
                "is_public": is_public,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            data["voices"].append(entry)
            self._write_data(data)
            return self._to_voice_entry(entry)

    async def list_available_voices(self, user_telegram_id: int) -> list[VoiceEntry]:
        async with self._lock:
            data = self._read_data()
            result = [
                self._to_voice_entry(raw)
                for raw in data["voices"]
                if raw["is_public"] or raw["owner_telegram_id"] == user_telegram_id
            ]
            result.sort(key=lambda item: (item.name.lower(), item.created_at))
            return result

    async def get_voice_by_entry_id(self, entry_id: str) -> VoiceEntry | None:
        async with self._lock:
            data = self._read_data()
            for raw in data["voices"]:
                if raw["entry_id"] == entry_id:
                    return self._to_voice_entry(raw)
            return None

    async def set_selected_voice(self, user_telegram_id: int, entry_id: str) -> bool:
        async with self._lock:
            data = self._read_data()
            for raw in data["voices"]:
                if raw["entry_id"] == entry_id and (
                    raw["is_public"] or raw["owner_telegram_id"] == user_telegram_id
                ):
                    data["selected_voice_by_user"][str(user_telegram_id)] = entry_id
                    self._write_data(data)
                    return True
            return False

    async def get_selected_voice(self, user_telegram_id: int) -> VoiceEntry | None:
        async with self._lock:
            data = self._read_data()
            selected_id = data["selected_voice_by_user"].get(str(user_telegram_id))
            if not selected_id:
                return None
            for raw in data["voices"]:
                if raw["entry_id"] == selected_id and (
                    raw["is_public"] or raw["owner_telegram_id"] == user_telegram_id
                ):
                    return self._to_voice_entry(raw)
            return None

    async def find_available_by_name(self, user_telegram_id: int, name: str) -> list[VoiceEntry]:
        lowered = name.strip().lower()
        if not lowered:
            return []
        voices = await self.list_available_voices(user_telegram_id)
        return [voice for voice in voices if voice.name.strip().lower() == lowered]

    async def sync_user_voices_from_provider(
        self,
        *,
        owner_telegram_id: int,
        provider_voices: list[dict[str, str]],
        provider_voice_ids_for_prune: list[str] | None = None,
    ) -> int:
        async with self._lock:
            data = self._read_data()
            created_or_updated = 0

            for voice in provider_voices:
                voice_id = str(voice.get("voice_id", "")).strip()
                name = str(voice.get("name", "")).strip()
                if not voice_id or not name:
                    continue

                existing = self._find_existing_voice(data["voices"], voice_id, owner_telegram_id)
                if existing is not None:
                    if existing["name"] != name:
                        existing["name"] = name
                        created_or_updated += 1
                    continue

                data["voices"].append(
                    {
                        "entry_id": str(uuid4()),
                        "name": name,
                        "voice_id": voice_id,
                        "owner_telegram_id": owner_telegram_id,
                        "is_public": False,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                created_or_updated += 1

            if provider_voice_ids_for_prune is not None:
                provider_ids = {item.strip() for item in provider_voice_ids_for_prune if item.strip()}
                removed_entry_ids: set[str] = set()
                retained_voices: list[dict[str, Any]] = []
                for raw in data["voices"]:
                    should_remove = (
                        raw.get("owner_telegram_id") == owner_telegram_id
                        and not bool(raw.get("is_public", False))
                        and str(raw.get("voice_id", "")).strip() not in provider_ids
                    )
                    if should_remove:
                        removed_entry_ids.add(str(raw.get("entry_id", "")))
                        created_or_updated += 1
                        continue
                    retained_voices.append(raw)
                data["voices"] = retained_voices
                if removed_entry_ids:
                    selected_id = data["selected_voice_by_user"].get(str(owner_telegram_id))
                    if selected_id in removed_entry_ids:
                        data["selected_voice_by_user"].pop(str(owner_telegram_id), None)

            self._write_data(data)
            return created_or_updated

    async def set_conversion_mode(self, user_telegram_id: int, mode: str) -> bool:
        normalized = mode.strip().lower()
        if normalized not in {"natural", "strong"}:
            return False
        async with self._lock:
            data = self._read_data()
            data["conversion_mode_by_user"][str(user_telegram_id)] = normalized
            self._write_data(data)
            return True

    async def get_conversion_mode(self, user_telegram_id: int) -> str:
        async with self._lock:
            data = self._read_data()
            mode = str(data["conversion_mode_by_user"].get(str(user_telegram_id), "strong")).lower()
            if mode not in {"natural", "strong"}:
                return "strong"
            return mode

    async def set_voice_method(self, user_telegram_id: int, method: str) -> bool:
        normalized = method.strip().lower()
        if normalized not in {"sts", "tts"}:
            return False
        async with self._lock:
            data = self._read_data()
            data["voice_method_by_user"][str(user_telegram_id)] = normalized
            self._write_data(data)
            return True

    async def get_voice_method(self, user_telegram_id: int) -> str:
        async with self._lock:
            data = self._read_data()
            method = str(data["voice_method_by_user"].get(str(user_telegram_id), "sts")).lower()
            if method not in {"sts", "tts"}:
                return "sts"
            return method

    async def set_sync_scope(self, user_telegram_id: int, scope: str) -> bool:
        normalized = scope.strip().lower()
        if normalized not in {"cloned", "all"}:
            return False
        async with self._lock:
            data = self._read_data()
            data["sync_scope_by_user"][str(user_telegram_id)] = normalized
            self._write_data(data)
            return True

    async def get_sync_scope(self, user_telegram_id: int) -> str:
        async with self._lock:
            data = self._read_data()
            scope = str(data["sync_scope_by_user"].get(str(user_telegram_id), "cloned")).lower()
            if scope not in {"cloned", "all"}:
                return "cloned"
            return scope

    async def set_response_mode(self, user_telegram_id: int, mode: str) -> bool:
        normalized = mode.strip().lower()
        if normalized not in {"auto", "voice", "audio"}:
            return False
        async with self._lock:
            data = self._read_data()
            data["response_mode_by_user"][str(user_telegram_id)] = normalized
            self._write_data(data)
            return True

    async def get_response_mode(self, user_telegram_id: int) -> str:
        async with self._lock:
            data = self._read_data()
            mode = str(data["response_mode_by_user"].get(str(user_telegram_id), "auto")).lower()
            if mode not in {"auto", "voice", "audio"}:
                return "auto"
            return mode

    async def ensure_allowed_users(self, user_ids: list[int]) -> None:
        normalized = sorted({int(item) for item in user_ids})
        async with self._lock:
            data = self._read_data()
            current = set(_to_int_list(data["allowed_user_ids"]))
            changed = False
            for user_id in normalized:
                if user_id not in current:
                    current.add(user_id)
                    changed = True
            if changed:
                data["allowed_user_ids"] = sorted(current)
                self._write_data(data)

    async def list_allowed_users(self) -> list[int]:
        async with self._lock:
            data = self._read_data()
            return sorted(_to_int_list(data["allowed_user_ids"]))

    async def is_user_allowed(self, user_id: int) -> bool:
        async with self._lock:
            data = self._read_data()
            return int(user_id) in set(_to_int_list(data["allowed_user_ids"]))

    async def allow_user(self, user_id: int) -> bool:
        target = int(user_id)
        async with self._lock:
            data = self._read_data()
            current = set(_to_int_list(data["allowed_user_ids"]))
            if target in current:
                return False
            current.add(target)
            data["allowed_user_ids"] = sorted(current)
            self._write_data(data)
            return True

    async def deny_user(self, user_id: int) -> bool:
        target = int(user_id)
        async with self._lock:
            data = self._read_data()
            current = set(_to_int_list(data["allowed_user_ids"]))
            if target not in current:
                return False
            current.remove(target)
            data["allowed_user_ids"] = sorted(current)
            self._write_data(data)
            return True

    @staticmethod
    def _find_existing_voice(
        voices: list[dict[str, Any]], voice_id: str, owner_telegram_id: int
    ) -> dict[str, Any] | None:
        for item in voices:
            if item["voice_id"] == voice_id and item["owner_telegram_id"] == owner_telegram_id:
                return item
        return None

    @staticmethod
    def _to_voice_entry(raw: dict[str, Any]) -> VoiceEntry:
        return VoiceEntry(
            entry_id=raw["entry_id"],
            name=raw["name"],
            voice_id=raw["voice_id"],
            owner_telegram_id=raw["owner_telegram_id"],
            is_public=raw["is_public"],
            created_at=raw["created_at"],
        )

    def _read_data(self) -> dict[str, Any]:
        if not self._file_path.exists():
            return {
                "voices": [],
                "selected_voice_by_user": {},
                "conversion_mode_by_user": {},
                "voice_method_by_user": {},
                "sync_scope_by_user": {},
                "response_mode_by_user": {},
                "allowed_user_ids": [],
            }

        with self._file_path.open("r", encoding="utf-8") as file:
            parsed = json.load(file)

        if "voices" not in parsed or not isinstance(parsed["voices"], list):
            parsed["voices"] = []
        if "selected_voice_by_user" not in parsed or not isinstance(
            parsed["selected_voice_by_user"], dict
        ):
            parsed["selected_voice_by_user"] = {}
        if "conversion_mode_by_user" not in parsed or not isinstance(
            parsed["conversion_mode_by_user"], dict
        ):
            parsed["conversion_mode_by_user"] = {}
        if "voice_method_by_user" not in parsed or not isinstance(parsed["voice_method_by_user"], dict):
            parsed["voice_method_by_user"] = {}
        if "sync_scope_by_user" not in parsed or not isinstance(parsed["sync_scope_by_user"], dict):
            parsed["sync_scope_by_user"] = {}
        if "response_mode_by_user" not in parsed or not isinstance(
            parsed["response_mode_by_user"], dict
        ):
            parsed["response_mode_by_user"] = {}
        if "allowed_user_ids" not in parsed or not isinstance(parsed["allowed_user_ids"], list):
            parsed["allowed_user_ids"] = []
        return parsed

    def _write_data(self, data: dict[str, Any]) -> None:
        with self._file_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)


def _to_int_list(values: list[Any]) -> list[int]:
    result: list[int] = []
    for item in values:
        try:
            result.append(int(item))
        except (ValueError, TypeError):
            continue
    return result
