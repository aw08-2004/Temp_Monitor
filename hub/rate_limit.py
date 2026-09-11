"""Lightweight in-memory rate limiter for sensitive endpoints.

No external dependencies. Uses a sliding-window counter per IP/key.
Thread-safe via a lock. Stale entries are pruned on each check.

Usage:
    from rate_limit import RateLimiter
    limiter = RateLimiter()

    @app.route('/api/report', methods=['POST'])
    @limiter.limit('60/minute')
    def report_temp():
        ...
"""
import re
import time
import threading
from functools import wraps
from collections import defaultdict

from flask import request, jsonify

_WINDOW_RE = re.compile(r'^(\d+)/(second|minute|hour)$')
_SECONDS = {'second': 1, 'minute': 60, 'hour': 3600}


class RateLimiter:
    """Sliding-window per-key rate limiter (in-memory, thread-safe)."""

    def __init__(self, app=None):
        self._buckets = defaultdict(list)  # key -> [timestamps]
        self._lock = threading.Lock()
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        """Register teardown to periodically prune stale entries."""
        app.teardown_appcontext(self._prune)

    def _prune(self, _exc=None):
        """Remove timestamps older than the largest window (1 hour)."""
        cutoff = time.time() - 3600
        with self._lock:
            stale_keys = []
            for key, stamps in self._buckets.items():
                self._buckets[key] = [t for t in stamps if t > cutoff]
                if not self._buckets[key]:
                    stale_keys.append(key)
            for key in stale_keys:
                del self._buckets[key]

    def limit(self, rule, key_func=None):
        """Decorator: enforce a rate limit on a Flask route.

        `rule` is 'N/second', 'N/minute', or 'N/hour'.
        `key_func` defaults to client IP via request.remote_addr.
        """
        m = _WINDOW_RE.match(rule)
        if not m:
            raise ValueError(f"Invalid rate limit rule: {rule!r}. Use 'N/second|minute|hour'.")
        max_count = int(m.group(1))
        window_seconds = _SECONDS[m.group(2)]

        def decorator(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                key = (key_func or self._default_key)(request)
                now = time.time()
                cutoff = now - window_seconds
                with self._lock:
                    stamps = self._buckets[key]
                    # prune old entries for this key
                    self._buckets[key] = [t for t in stamps if t > cutoff]
                    if len(self._buckets[key]) >= max_count:
                        return jsonify({"error": "Rate limit exceeded", "retry_after": window_seconds}), 429
                    self._buckets[key].append(now)
                return fn(*args, **kwargs)
            return wrapper
        return decorator

    @staticmethod
    def _default_key(req):
        """Best-effort client identifier, respecting X-Forwarded-For behind a proxy."""
        forwarded = req.headers.get('X-Forwarded-For', '')
        if forwarded:
            return forwarded.split(',')[0].strip()
        return req.remote_addr or '0.0.0.0'
