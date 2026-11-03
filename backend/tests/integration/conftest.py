"""Integration test scaffolding: env is fixed BEFORE app modules import.

The engine builds at import time from ``DATABASE_URL`` (Settings lru_cache),
so this module must not import anything from ``backend.app`` at all — only
set the environment the tests need.
"""

from __future__ import annotations

import os
import tempfile

_db_path = os.path.join(tempfile.mkdtemp(prefix="fd-test-"), "integration.db")

os.environ.setdefault("DATABASE_URL", "sqlite:///" + _db_path)
os.environ.setdefault("PAYPAL_CLIENT_ID", "test-client-id")
os.environ.setdefault("PAYPAL_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("DEMO_KEY", "test-demo-key-0123456789abcdef")
os.environ.setdefault("PAYPAL_API_BASE_URL", "https://api-m.sandbox.paypal.com")
os.environ.setdefault("PAYPAL_WEBHOOK_ID", "wh-test-id")
