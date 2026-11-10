"""JSON helpers with the call shape the agoda module uses: orjson when it is
installed (faster on Agoda's 300 KB+ hotel records), the standard library
otherwise, with identical results either way.

    obj = fastjson.loads(text_or_bytes)
    text = fastjson.dumps(obj)          # compact, non-ASCII kept as-is
"""
import json as _json

try:
    import orjson
except ImportError:
    orjson = None


def loads(s):
    """Parse JSON text or bytes."""
    if orjson is None:
        return _json.loads(s)
    if type(s) not in (str, bytes):
        s = str(s) if isinstance(s, str) else bytes(s)
    try:
        return orjson.loads(s)
    except _json.JSONDecodeError:
        # orjson is stricter than the stdlib (NaN, deep nesting); the stdlib decides
        return _json.loads(s)


def dumps(obj):
    """Compact JSON text, non-ASCII unescaped."""
    if orjson is None:
        return _json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    try:
        return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS).decode("utf-8")
    except TypeError:
        return _json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
