from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from backend.app.settings import Settings
from backend.app.store import StateStore
from backend.app.sz159502_engine import SZ159502Engine
from backend.app.sz_market_client import SZPaperMarketClient
from backend.app.sz_portfolio import SZPortfolioStore
from backend.app.tonghuashun_market import demo_snapshot, tonghuashun_status
from shared.quant_core import SZ_TZ, normalize_ohlcv, run_sz159502_backtest, validate_sz159502_backtest_prerequisites


def test_sz_normalize_filters_shenzhen_session() -> None:
    # 09:25 should be filtered, 09:30 kept, 11:30 edge, 12:00 filtered, 13:00 kept, 15:00 filtered? Actually 15:00 not included (bar open)
    frame = pd.DataFrame({
        "datetime": ["2024-01-02 09:25", "2024-01-02 09:30", "2024-01-02 11:25", "2024-01-02 11:30", "2024-01-02 12:00", "2024-01-02 13:00", "2024-01-02 14:55", "2024-01-02 15:00"],
        "open": [1]*8, "high": [1.1]*8, "low": [0.9]*8, "close": [1]*8, "volume": [100]*8, "symbol": ["159502.SZ"]*8,
    })
    out = normalize_ohlcv(frame, source_timezone="Asia/Shanghai", market="sz")
    # Expect 09:30, 11:25, 13:00, 14:55 -> 4 rows (11:30 is not a 5min bar open, 12:00 out, 15:00 out)
    assert len(out) == 4
    times = out["timestamp"].dt.tz_convert(SZ_TZ).dt.strftime("%H:%M").tolist()
    assert times == ["09:30", "11:25", "13:00", "14:55"]


def test_cost_is_placeholder_and_not_real_rate() -> None:
    settings = Settings(_env_file=None)
    status = tonghuashun_status(settings)
    assert status["cost"]["is_placeholder"] is True
    assert "非真实费率" in status["cost"]["note"]
    assert status["cost"]["slippage_bps"] == settings.sz159502_slippage_bps
    # No key leak
    assert "tonghuashun_api_key" not in str(status).lower() or "has_key" in str(status).lower()
    assert status["tonghuashun"]["has_key_on_server"] is False
    assert status["tonghuashun"]["enabled"] is False
    snapshot = demo_snapshot(settings)
    assert snapshot["is_offline_demo"] is True
    assert snapshot["is_real_api"] is False


def test_sz_backtest_requires_authorization() -> None:
    with pytest.raises(ValueError, match="未获授权"):
        validate_sz159502_backtest_prerequisites(False)
    # with authorized flag true, but still no bars, run should fail for no bars not for authorization
    closes = [1.0, 1.01, 1.02, 1.01, 1.0, 1.02, 1.05]
    stamps = pd.date_range("2024-01-02 09:30", periods=len(closes), freq="5min", tz="Asia/Shanghai").tz_convert("UTC")
    bars = pd.DataFrame({"timestamp": stamps, "symbol": "159502.SZ", "open": closes, "high": [c+0.01 for c in closes], "low": [c-0.01 for c in closes], "close": closes, "volume": [1000]*len(closes)})
    with pytest.raises(ValueError, match="未获授权"):
        run_sz159502_backtest(bars, has_authorized_5min_history=False)
    # authorized true should allow (even if small sample)
    # Need enough bars for SMA20/50, so use small windows for test
    from shared.quant_core import BacktestConfig
    cfg = BacktestConfig(fast_window=2, slow_window=3, initial_cash=100_000, per_symbol_equity_fraction=0.10, max_gross_exposure_fraction=0.5, market="sz", currency="CNY", cost_note="占位")
    result = run_sz159502_backtest(bars, config=cfg, has_authorized_5min_history=True)
    assert "metrics" in result


def test_daily_drawdown_blocks_buy_but_allows_sell(tmp_path) -> None:
    async def scenario() -> None:
        settings = Settings(_env_file=None, sz159502_poll_enabled=False, tonghuashun_enabled=False, sz159502_has_authorized_5min_history=False)
        # Build bars with BUY cross
        closes = [1.0, 0.9, 0.8, 0.7, 0.8, 1.0]
        stamps = pd.date_range("2024-01-02 09:30", periods=len(closes), freq="5min", tz="Asia/Shanghai").tz_convert("UTC")
        bars = pd.DataFrame({"timestamp": stamps, "symbol": "159502.SZ", "open": closes, "high": [c+0.02 for c in closes], "low": [c-0.02 for c in closes], "close": closes, "volume": [1000]*len(closes)})
        portfolio = SZPortfolioStore(str(tmp_path / "sz_portfolio.db"), settings)
        # Simulate holdings: buy 1000 shares at 1.0
        portfolio.submit_market_order("159502.SZ", 1000, "buy", "init-buy", price=1.0)
        # Force drawdown: equity from 100k to 97k (simulate loss)
        # Set cash low to trigger drawdown: manually set cash to make equity < baseline *0.98
        # Baseline will be initial 100k (first day), equity after holdings with current price ~1.0 => ~100k - commission + MV
        # To trigger, we need to set baseline high and equity low: we can create a scenario where account last_equity high, equity low.
        # Use SZPaperMarketClient with demo_bars ending at 0.9 (down)
        # Instead test engine logic directly: create a fake client that returns low equity
        class FakeClient:
            def __init__(self, bars, equity, cash):
                self.bars = bars
                self.equity = equity
                self.cash = cash
                self.positions_list = [{"symbol": "159502.SZ", "qty": 1000, "side": "long", "avg_entry_price": 1.0, "current_price": 0.9, "market_value": 900, "unrealized_pl": -100, "unrealized_plpc": -0.1}]
                self.submitted = []
            def market_clock(self): return {"is_open": False}
            def account_snapshot(self): return {"status": "ACTIVE", "cash": self.cash, "equity": self.equity, "last_equity": 100_000, "buying_power": self.cash, "portfolio_value": 900, "currency": "CNY", "trading_blocked": False}
            def positions(self): return self.positions_list
            def open_orders(self): return []
            def latest_bars(self, symbols, lookback_minutes=1000): return self.bars.copy()
            def submit_market_order(self, symbol, qty, side, client_order_id):
                self.submitted.append({"symbol": symbol, "qty": qty, "side": side, "client_order_id": client_order_id})
                return {"id": "order-1", "symbol": symbol, "qty": qty, "side": side, "client_order_id": client_order_id, "status": "accepted"}
        # Trigger drawdown: equity 97000 < 100000*0.98 =98000
        client = FakeClient(bars, equity=97_000, cash=96_100)
        store = StateStore(str(tmp_path / "sz_engine.db"))
        # Set prior watermark to second last bar so next poll will see BUY signal
        store.set_watermark("159502.SZ", bars.iloc[-2]["timestamp"].isoformat())
        # Force allow polling even though disabled flag is False: temporarily enable for test
        settings.sz159502_poll_enabled = True
        settings.tonghuashun_enabled = True
        settings.tonghuashun_api_key = "dummy-for-test"
        engine = SZ159502Engine(settings, client, store)
        await engine._poll_once()
        # 当日回撤后：BUY 被拦截，但风控会尝试卖出可卖持仓（即便无 SELL 信号）
        # 本测试持仓 1000 股且回撤触发，应触发风控卖出而非买入
        assert len(client.submitted) == 1
        assert client.submitted[0]["side"] == "sell"
        assert "risk" in client.submitted[0]["client_order_id"]
        state = store.engine_state()
        assert state["paused_reason"] == "daily_loss_limit"
        assert "停止新买入" in state["note"]
        # Now test SELL allowed: create bars with SELL signal while drawdown
        closes_sell = [1.0, 1.1, 1.2, 1.3, 1.2, 1.0]
        stamps2 = pd.date_range("2024-01-02 13:00", periods=len(closes_sell), freq="5min", tz="Asia/Shanghai").tz_convert("UTC")
        bars2 = pd.DataFrame({"timestamp": stamps2, "symbol": "159502.SZ", "open": closes_sell, "high": [c+0.02 for c in closes_sell], "low": [c-0.02 for c in closes_sell], "close": closes_sell, "volume": [1000]*len(closes_sell)})
        client2 = FakeClient(bars2, equity=97_000, cash=96_100)
        store2 = StateStore(str(tmp_path / "sz_engine2.db"))
        store2.set_watermark("159502.SZ", bars2.iloc[-2]["timestamp"].isoformat())
        engine2 = SZ159502Engine(settings, client2, store2)
        await engine2._poll_once()
        # SELL should be allowed even during drawdown, so order submitted
        assert len(client2.submitted) == 1
        assert client2.submitted[0]["side"] == "sell"
    asyncio.run(scenario())
