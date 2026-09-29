from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def tri_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def nonempty(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if nonempty(value):
            return value
    return ""


def walk_dicts(node: Any) -> Iterable[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_dicts(value)


def row_get(row: Any, key: str, default: Any = None) -> Any:
    getter = getattr(row, "get", None)
    if callable(getter):
        return getter(key, default)
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default
