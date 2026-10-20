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

    # 深圳 ETF 159502 单标的纸面模拟器（嘉实标普生物科技精选行业 ETF QDII）
    # 所有成本字段均为可配置占位值，不得声称为真实费率；离线/Demo 需醒目标注。
    sz159502_symbol: str = "159502.SZ"
    sz159502_name: str = "嘉实标普生物科技精选行业 ETF（QDII）"
    sz159502_currency: Literal["CNY"] = "CNY"
    sz159502_market: str = "SZSE"
    sz159502_timezone: str = "Asia/Shanghai"
    sz159502_initial_equity: float = 100_000.0
    sz159502_per_trade_fraction: float = 0.10
    sz159502_daily_loss_limit_fraction: float = 0.02
    sz159502_slippage_bps: float = 5.0
    sz159502_commission_per_order: float = 0.0
    sz159502_cost_note: str = "成本为可配置占位值，非真实费率，仅用于纸面演示"
    sz159502_has_authorized_5min_history: bool = False
    # 同花顺对接：Key 仅从服务端环境变量 TONGHUASHUN_API_KEY 读取，前端/浏览器/GitHub 绝不保存
    tonghuashun_api_key: str = Field(default="", validation_alias="TONGHUASHUN_API_KEY")
    tonghuashun_enabled: bool = False
    sz159502_poll_enabled: bool = False
    sz159502_poll_seconds: int = 60

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
        # SZ 159502 验证：仓位与风控参数
        if not (0 < self.sz159502_per_trade_fraction <= 1):
            raise ValueError("sz159502_per_trade_fraction must be in (0, 1]")
        if not (0 < self.sz159502_daily_loss_limit_fraction < 1):
            raise ValueError("sz159502_daily_loss_limit_fraction must be in (0, 1)")
        if self.sz159502_poll_seconds < 10:
            raise ValueError("sz159502_poll_seconds must be at least 10 seconds")
        if self.sz159502_initial_equity <= 0:
            raise ValueError("sz159502_initial_equity must be positive")
        if self.sz159502_slippage_bps < 0 or self.sz159502_slippage_bps >= 10_000:
            raise ValueError("sz159502_slippage_bps must be in [0, 10000)")
        if self.sz159502_commission_per_order < 0:
            raise ValueError("sz159502_commission_per_order must be non-negative")
        # 同花顺接口默认关闭；启用时必须显式提供服务端环境变量 Key
        if self.tonghuashun_enabled and not self.tonghuashun_api_key:
            raise ValueError("tonghuashun_enabled requires TONGHUASHUN_API_KEY on the server")
        if self.sz159502_poll_enabled and not self.tonghuashun_enabled:
            raise ValueError("sz159502_poll_enabled requires tonghuashun_enabled=true")
        if self.sz159502_poll_enabled and not self.tonghuashun_api_key:
            raise ValueError("sz159502 polling requires TONGHUASHUN_API_KEY")
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
