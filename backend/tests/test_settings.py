from __future__ import annotations

import pytest

from backend.app.alpaca_paper import PaperOnlyViolation
from backend.app.alpaca_paper import AlpacaPaperClient
from backend.app.settings import PAPER_TRADING_URL, Settings


def test_demo_is_default_and_has_no_credentials() -> None:
    settings = Settings(_env_file=None)
    assert settings.app_mode == "demo"
    assert not settings.alpaca_api_key


def test_paper_mode_rejects_missing_credentials() -> None:
    with pytest.raises(ValueError, match="requires server-side Alpaca Paper credentials"):
        Settings(_env_file=None, app_mode="paper", app_password="long-private-test-password", session_secret="s" * 40)


def test_paper_mode_rejects_live_or_unknown_endpoint() -> None:
    with pytest.raises(ValueError, match="refuses any non-Paper"):
        Settings(
            _env_file=None, app_mode="paper", app_password="long-private-test-password",
            session_secret="s" * 40, alpaca_api_key="test-key", alpaca_api_secret="test-secret",
            alpaca_trading_url="https://api.alpaca.markets",
        )


def test_production_rejects_insecure_cookie() -> None:
    with pytest.raises(ValueError, match="SESSION_COOKIE_SECURE=true"):
        Settings(
            _env_file=None, environment="production", app_mode="paper",
            app_password="long-private-test-password", session_secret="s" * 40,
            alpaca_api_key="test-key", alpaca_api_secret="test-secret",
            alpaca_trading_url=PAPER_TRADING_URL, session_cookie_secure=False,
        )


def test_production_demo_requires_https_and_strong_login_secrets() -> None:
    with pytest.raises(ValueError, match="APP_PUBLIC_URL with an https:// URL"):
        Settings(_env_file=None, environment="production", app_mode="demo",
                 app_password="a-long-production-password", session_secret="s" * 40,
                 session_cookie_secure=True)
    settings = Settings(_env_file=None, environment="production", app_mode="demo",
                       app_password="a-long-production-password", session_secret="s" * 40,
                       session_cookie_secure=True, app_public_url="https://quant.example.com")
    assert settings.app_public_url.startswith("https://")


def test_adapter_refuses_demo_mode() -> None:
    with pytest.raises(PaperOnlyViolation, match="app_mode=paper"):
        AlpacaPaperClient(Settings(_env_file=None))
