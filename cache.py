"""A tiny in-memory response cache with the call shape the agoda module
uses. Results live in this process only and expire after `ttl` (a
datetime.timedelta, see agoda/cache_config.py), so asking for the same
search, hotel or review page again within it costs no request to Agoda —
and a hotel's details and reviews share one review-summary call.

`ttl` may also be a function (data, result) -> timedelta | None that
decides per result (None = don't keep this one).

Each hit is a fresh copy (stored as JSON), so a caller that edits the result
(routes.paginate pops `pagination`) never changes what the next caller gets.
"""
import json
import threading
import time

_store = {}
_lock = threading.Lock()
MAX_ENTRIES = 2000


def _key(namespace, data):
    return namespace + ":" + json.dumps(data, sort_keys=True, default=str)


def cached_call(namespace, data, fn, ttl, cache=True, refresh=False):
    """Serve fn(**data) from memory for `ttl`, else run it and keep the result.
    Exceptions from fn propagate uncached."""
    if not (cache and ttl):
        return fn(**data)
    key = _key(namespace, data)
    now = time.time()
    if not refresh:
        with _lock:
            hit = _store.get(key)
        if hit and hit[0] > now:
            return json.loads(hit[1])
    result = fn(**data)
    keep = ttl(data, result) if callable(ttl) else ttl
    if not keep:
        return result
    with _lock:
        if len(_store) >= MAX_ENTRIES:
            for stale in [k for k, (expires, _) in _store.items() if expires <= now] or list(_store)[:100]:
                _store.pop(stale, None)
        _store[key] = (now + keep.total_seconds(), json.dumps(result, ensure_ascii=False))
    return result
