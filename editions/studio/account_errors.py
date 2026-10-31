"""Confirmed account authentication failures, shared by UI and HTTP workers."""


class LoginExpiredError(RuntimeError):
    """Dola explicitly reported a login wall or an unauthenticated response."""
