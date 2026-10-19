from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "production"] = "development"
    app_mode: Literal["demo", "paper"] = "demo"
    app_public_url: str = "http://localhost:5173"
    app_password: str = ""
    session_secret: str = "development-only-change-me-before-production-000000"
    session_cookie_secure: bool = False
    database_path: str = "./data/quant.db"

    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_trading_url: str = PAPER_TRADING_URL
    alpaca_data_feed: Literal["iex"] = "iex"

    strategy_symbols: list[str] = Field(default_factory=lambda: ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"])
    fast_window: int = 20
    slow_window: int = 50
    initial_equity: float = 100_000.0
    per_symbol_fraction: float = 0.10
    max_gross_fraction: float = 0.50
    daily_loss_limit_fraction: float = 0.02
    slippage_bps: float = 5.0
    commission_per_order: float = 0.0
    auto_resume: bool = True
    poll_seconds: int = 30

    @model_validator(mode="after")
    def validate_safety(self) -> "Settings":
        if not (0 < self.fast_window < self.slow_window):
            raise ValueError("fast_window must be positive and less than slow_window")
        if not (0 < self.per_symbol_fraction <= 1 and 0 < self.max_gross_fraction <= 1):
            raise ValueError("position fractions must be in (0, 1]")
        if not (0 < self.daily_loss_limit_fraction < 1):
            raise ValueError("daily_loss_limit_fraction must be in (0, 1)")
        if self.poll_seconds < 10:
            raise ValueError("poll_seconds must be at least 10 seconds")
        self.strategy_symbols = list(dict.fromkeys(s.strip().upper() for s in self.strategy_symbols if s.strip()))
        if not self.strategy_symbols or len(self.strategy_symbols) > 30:
            raise ValueError("strategy_symbols must contain between 1 and 30 symbols")
        if self.app_mode == "paper":
            if self.alpaca_trading_url.rstrip("/") != PAPER_TRADING_URL:
                raise ValueError("Paper mode refuses any non-Paper trading endpoint")
            if not self.alpaca_api_key or not self.alpaca_api_secret:
                raise ValueError("Paper mode requires server-side Alpaca Paper credentials")
            if len(self.app_password) < 16:
                raise ValueError("Paper mode requires APP_PASSWORD with at least 16 characters")
            if len(self.session_secret) < 32 or self.session_secret.startswith("development-only"):
                raise ValueError("Paper mode requires a unique SESSION_SECRET with at least 32 characters")
            if self.environment == "production" and not self.session_cookie_secure:
                raise ValueError("production Paper mode requires SESSION_COOKIE_SECURE=true")
        if self.environment == "production":
            if len(self.app_password) < 16:
                raise ValueError("production mode requires APP_PASSWORD with at least 16 characters")
            if len(self.session_secret) < 32 or self.session_secret.startswith("development-only"):
                raise ValueError("production mode requires a unique SESSION_SECRET with at least 32 characters")
            if not self.app_public_url.startswith("https://"):
                raise ValueError("production mode requires APP_PUBLIC_URL with an https:// URL")
            if not self.session_cookie_secure:
                raise ValueError("production mode requires SESSION_COOKIE_SECURE=true")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
