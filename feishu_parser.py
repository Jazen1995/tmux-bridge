"""Extract supported user text from Feishu message payloads."""

from __future__ import annotations

import json
from typing import Any


def extract_message_text(message_type: str, content: str) -> str:
    try:
        payload: dict[str, Any] = json.loads(content or "{}")
    except json.JSONDecodeError:
        return ""
    if message_type == "text":
        return (payload.get("text") or "").strip()
    if message_type == "post":
        pieces: list[str] = []
        for paragraph in payload.get("content") or []:
            for element in paragraph:
                if element.get("tag") in {"text", "a"}:
                    pieces.append(element.get("text") or element.get("href") or "")
        return "".join(pieces).strip()
    return ""


def sender_is_allowed(
    *,
    open_id: str | None,
    union_id: str | None,
    owner_open_id: str | None,
    owner_union_id: str | None,
) -> bool:
    """Authorize across apps with union_id, retaining open_id compatibility."""
    if owner_union_id:
        return bool(union_id and union_id == owner_union_id)
    if owner_open_id:
        return bool(open_id and open_id == owner_open_id)
    return False
