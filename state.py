"""Persistent chat-to-thread bindings."""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: str, *, default_cwd: str):
        self.path = Path(path)
        self.default_cwd = default_cwd
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._data = {}
                return
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._data = {}
                return
            self._data = value if isinstance(value, dict) else {}

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return deepcopy(self._data)

    def get(self, chat_id: str) -> dict[str, Any]:
        with self._lock:
            value = deepcopy(self._data.get(chat_id, {}))
        value.setdefault("cwd", self.default_cwd)
        value.setdefault("thread_id", None)
        value.setdefault("thread_name", None)
        return value

    def update(self, chat_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            current = self._data.setdefault(chat_id, {"cwd": self.default_cwd})
            current.update(changes)
            result = deepcopy(current)
            self._save_locked()
            return result

    def unbind(self, chat_id: str) -> None:
        self.update(chat_id, thread_id=None, thread_name=None)

    def chats_for_thread(self, thread_id: str) -> list[str]:
        with self._lock:
            return [
                chat_id for chat_id, value in self._data.items()
                if value.get("thread_id") == thread_id
            ]

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
