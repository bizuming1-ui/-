from __future__ import annotations

from functools import lru_cache
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, status

from backend.app.settings import Settings

_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_FAILURES = 5
_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def login_rate_limited(address: str) -> bool:
    now = time.monotonic()
    with _login_lock:
        attempts = [t for t in _login_failures.get(address, []) if now - t < _LOGIN_WINDOW_SECONDS]
        if attempts:
            _login_failures[address] = attempts
        else:
            _login_failures.pop(address, None)
        return len(attempts) >= _LOGIN_MAX_FAILURES


def record_login_failure(address: str) -> None:
    now = time.monotonic()
    with _login_lock:
        attempts = [t for t in _login_failures.get(address, []) if now - t < _LOGIN_WINDOW_SECONDS]
        attempts.append(now)
        _login_failures[address] = attempts
        if len(_login_failures) > 4096:
            for key, values in list(_login_failures.items()):
                recent = [t for t in values if now - t < _LOGIN_WINDOW_SECONDS]
                if recent:
                    _login_failures[key] = recent
                else:
                    _login_failures.pop(key, None)
            while len(_login_failures) > 4096:
                _login_failures.pop(next(iter(_login_failures)))


def clear_login_failures(address: str) -> None:
    with _login_lock:
        _login_failures.pop(address, None)


@lru_cache(maxsize=4)
def _password_hash(password: str) -> str:
    """Hash the server-injected password once per process; never return it to clients."""
    return PasswordHasher().hash(password)


def verify_password(candidate: str, settings: Settings) -> bool:
    if not settings.app_password:
        return False
    try:
        return PasswordHasher().verify(_password_hash(settings.app_password), candidate)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def require_authenticated(request: Request, settings: Settings) -> None:
    # Local Demo preview is intentionally usable without credentials. Production
    # and all Paper deployments are validated to require an application password.
    if settings.environment == "development" and settings.app_mode == "demo" and not settings.app_password:
        return
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已失效，请重新登录")
