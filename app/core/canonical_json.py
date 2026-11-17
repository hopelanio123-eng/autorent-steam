from __future__ import annotations

import json
from typing import Any


def canonical_dumps(obj: Any) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def canonical_loads(data: bytes) -> dict:
    return json.loads(data.decode("utf-8"))
