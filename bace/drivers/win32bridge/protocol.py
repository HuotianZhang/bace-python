"""Encode bridge requests and replies as one UTF-8 JSON object per line.

Requests contain `id`, `target`, `method` and `args`. Replies echo `id`
and return `ok` with `result`, or `ok=False` with `error` and `type`.
The helper dispatches only explicitly listed methods. It does not unpickle,
evaluate code or import modules from a request, limiting a stray connection
to the exposed instrument operations."""
from __future__ import annotations

import json
from typing import Any

ENCODING = "utf-8"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8731


def encode(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode(ENCODING)


def decode(line: bytes) -> dict[str, Any]:
    return json.loads(line.decode(ENCODING))
