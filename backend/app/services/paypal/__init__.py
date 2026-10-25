"""PayPal REST clients (owned by Person 1). Every server-to-server PayPal call lives here."""

from .auth import PayPalAuthError, PayPalAuthManager, get_auth_manager

__all__ = ["PayPalAuthError", "PayPalAuthManager", "get_auth_manager"]
