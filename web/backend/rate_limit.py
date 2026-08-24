"""Shared slowapi Limiter instance.

Split out from `app.py` so routers can `@limiter.limit(...)` their own
endpoints without importing the application factory module (which itself
imports every router — a circular import otherwise).
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
