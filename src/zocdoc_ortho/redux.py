from __future__ import annotations

import ast
import json


def _decode_js_string_literal(text: str, quote_start: int) -> tuple[str, int]:
    quote = text[quote_start]
    i = quote_start + 1
    while i < len(text):
        if text[i] == quote:
            backslashes = 0
            j = i - 1
            while j > quote_start and text[j] == "\\":
                backslashes += 1
                j -= 1
            if backslashes % 2 == 0:
                literal = text[quote_start : i + 1]
                if quote == '"':
                    return json.loads(literal), i + 1
                return ast.literal_eval(literal), i + 1
        i += 1
    raise ValueError("Unterminated JavaScript string literal")


def extract_redux_state(html: str) -> dict | None:
    """Decode ``window.__REDUX_STATE__ = JSON.parse("...")`` from saved HTML."""
    search_from = 0
    while True:
        marker = html.find("window.__REDUX_STATE__", search_from)
        if marker < 0:
            return None

        parse_at = html.find("JSON.parse(", marker)
        if parse_at < 0:
            search_from = marker + 1
            continue

        i = parse_at + len("JSON.parse(")
        while i < len(html) and html[i].isspace():
            i += 1

        if i >= len(html) or html[i] not in {'"', "'"}:
            search_from = marker + 1
            continue

        try:
            encoded_json, _ = _decode_js_string_literal(html, i)
            value = json.loads(encoded_json)
            return value if isinstance(value, dict) else None
        except (ValueError, SyntaxError):
            search_from = marker + 1
