"""Wire format for the 32-bit bridge: one JSON object per line, UTF-8.

Request   {"id": int, "target": str, "method": str, "args": {...}}
Response  {"id": int, "ok": true,  "result": ...}
          {"id": int, "ok": false, "error": str, "type": str}

Deliberately boring: no pickling, no eval, no dynamic import. The helper
dispatches to an explicit table of methods, so a stray connection cannot make it
do anything the rig would not.
"""
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
