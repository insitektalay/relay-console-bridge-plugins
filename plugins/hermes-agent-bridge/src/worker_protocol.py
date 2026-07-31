from __future__ import annotations

import json
from typing import Any


MAX_WORKER_MESSAGE_BYTES = 4 * 1024 * 1024


def encode_worker_message(message: dict[str, Any]) -> bytes:
    payload = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf8")
    if len(payload) > MAX_WORKER_MESSAGE_BYTES:
        raise ValueError("Hermes profile worker message exceeds the size limit")
    return payload + b"\n"


def decode_worker_message(payload: bytes | str) -> dict[str, Any]:
    raw = payload.encode("utf8") if isinstance(payload, str) else payload
    if len(raw) > MAX_WORKER_MESSAGE_BYTES:
        raise ValueError("Hermes profile worker message exceeds the size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Hermes profile worker message must be an object")
    return value
