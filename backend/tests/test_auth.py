from __future__ import annotations

from backend.app.security import clear_login_failures, login_rate_limited, record_login_failure, verify_password
from backend.app.settings import Settings


def test_password_hash_verification() -> None:
    settings = Settings(_env_file=None, app_password="correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", settings)
    assert not verify_password("wrong-password", settings)


def test_login_rate_limit_locks_after_five_failures_and_can_be_cleared() -> None:
    address = "test-rate-limit-address"
    clear_login_failures(address)
    assert not login_rate_limited(address)
    for _ in range(5):
        record_login_failure(address)
    assert login_rate_limited(address)
    clear_login_failures(address)
    assert not login_rate_limited(address)
