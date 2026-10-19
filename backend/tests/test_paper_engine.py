from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from backend.app.paper_engine import EnginePreflightError, PaperEngine
from backend.app.settings import Settings
from backend.app.store import StateStore


class FakePaperClient:
    def __init__(self, bars: pd.DataFrame, *, equity: float = 10_000, last_equity: float = 10_000,
                 cash: float = 10_000, positions: list[dict] | None = None,
                 open_orders: list[dict] | None = None, market_open: bool = False) -> None:
        self.bars = bars
        self.equity = equity
        self.last_equity = last_equity
        self.cash = cash
        self.position_rows = positions or []
        self.open_order_rows = open_orders or []
        self.market_open = market_open
        self.submitted: list[dict] = []

    def market_clock(self) -> dict:
        return {"is_open": self.market_open}

    def account_snapshot(self) -> dict:
        return {"status": "ACTIVE", "cash": self.cash, "equity": self.equity,
                "last_equity": self.last_equity, "buying_power": self.cash,
                "portfolio_value": self.equity, "currency": "USD", "trading_blocked": False}

    def positions(self) -> list[dict]:
        return list(self.position_rows)

    def open_orders(self) -> list[dict]:
        return list(self.open_order_rows)

    def latest_bars(self, symbols: list[str], lookback_minutes: int = 1000) -> pd.DataFrame:
        return self.bars.copy()

    def submit_market_order(self, symbol: str, qty: int, side: str, client_order_id: str) -> dict:
        order = {"id": f"order-{len(self.submitted)+1}", "symbol": symbol, "qty": qty,
                 "side": side, "client_order_id": client_order_id, "status": "accepted"}
        self.submitted.append(order)
        return order


def paper_settings(**overrides) -> Settings:
    values = dict(_env_file=None, app_mode="paper", app_password="a-private-password-long-enough",
                  session_secret="x" * 48, alpaca_api_key="paper-key", alpaca_api_secret="paper-secret",
                  strategy_symbols=["AAPL"], fast_window=2, slow_window=3,
                  initial_equity=10_000, per_symbol_fraction=.20, max_gross_fraction=.50)
    values.update(overrides)
    return Settings(**values)


def bars_with_buy_cross() -> pd.DataFrame:
    closes = [10, 9, 8, 7, 8, 10]
    opens = [10, 9, 8, 7, 8, 10]
    stamps = pd.date_range("2024-01-02 09:30", periods=len(closes), freq="5min", tz="America/New_York").tz_convert("UTC")
    return pd.DataFrame({"timestamp": stamps, "symbol": "AAPL", "open": opens,
                         "high": [x + .2 for x in closes], "low": [x - .2 for x in closes],
                         "close": closes, "volume": [1000] * len(closes)})


def test_engine_emits_one_idempotent_order_after_completed_signal_bar(tmp_path) -> None:
    async def scenario() -> None:
        bars = bars_with_buy_cross()
        broker = FakePaperClient(bars)
        store = StateStore(str(tmp_path / "engine.sqlite3"))
        store.set_watermark("AAPL", bars.iloc[-2]["timestamp"].isoformat())
        engine = PaperEngine(paper_settings(), broker, store)
        await engine._poll_once()
        await engine._poll_once()
        assert len(broker.submitted) == 1
        assert broker.submitted[0]["side"] == "buy"
        assert broker.submitted[0]["qty"] > 0
        assert store.recent_events(1)[0]["state"] == "submitted"
    asyncio.run(scenario())


def test_daily_loss_limit_latches_and_disables_new_orders(tmp_path) -> None:
    async def scenario() -> None:
        broker = FakePaperClient(bars_with_buy_cross(), equity=9_700, last_equity=10_000)
        store = StateStore(str(tmp_path / "risk.sqlite3"))
        store.set_engine(True, "running")
        engine = PaperEngine(paper_settings(daily_loss_limit_fraction=.02), broker, store)
        await engine._poll_once()
        state = store.engine_state()
        assert state["enabled"] == 0
        assert state["paused_reason"] == "daily_loss_limit"
        assert broker.submitted == []
    asyncio.run(scenario())


def test_stale_market_data_during_open_session_pauses_engine(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pd.Timestamp, "now", classmethod(lambda cls, tz=None: pd.Timestamp("2024-01-02 15:30", tz="UTC")))
    async def scenario() -> None:
        broker = FakePaperClient(bars_with_buy_cross(), market_open=True)
        store = StateStore(str(tmp_path / "stale.sqlite3"))
        store.set_engine(True, "running")
        engine = PaperEngine(paper_settings(), broker, store)
        await engine._poll_once()
        state = store.engine_state()
        assert state["enabled"] == 0
        assert state["paused_reason"] == "stale_market_data"
        assert broker.submitted == []
    asyncio.run(scenario())


def test_start_refuses_unmanaged_open_orders(tmp_path) -> None:
    async def scenario() -> None:
        broker = FakePaperClient(bars_with_buy_cross(), open_orders=[{"id": "manual-order"}])
        store = StateStore(str(tmp_path / "preflight.sqlite3"))
        engine = PaperEngine(paper_settings(), broker, store)
        with pytest.raises(EnginePreflightError, match="未完成订单"):
            await engine.start()
        assert not engine.enabled
        assert not broker.submitted
    asyncio.run(scenario())


def test_restart_primes_current_bar_and_does_not_replay_old_signal(tmp_path) -> None:
    async def scenario() -> None:
        bars = bars_with_buy_cross()
        broker = FakePaperClient(bars)
        store = StateStore(str(tmp_path / "restart.sqlite3"))
        store.set_engine(True, "running")
        engine = PaperEngine(paper_settings(), broker, store)
        result = await engine.resume_after_restart()
        assert result["enabled"] is True
        await engine.stop()
        assert broker.submitted == []
        assert store.watermark("AAPL") == bars.iloc[-1]["timestamp"].isoformat()
    asyncio.run(scenario())
