from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from backend.app.settings import PAPER_TRADING_URL, Settings


class PaperOnlyViolation(RuntimeError):
    pass


class AlpacaPaperClient:
    """Small adapter for Alpaca's Paper account and IEX market-data feed.

    The adapter hard-codes the SDK's paper=True mode and refuses a non-Paper
    trading URL. It intentionally has no Live trading path.
    """

    def __init__(self, settings: Settings) -> None:
        if settings.app_mode != "paper":
            raise PaperOnlyViolation("Alpaca adapter can only be initialized in app_mode=paper")
        if settings.alpaca_trading_url.rstrip("/") != PAPER_TRADING_URL:
            raise PaperOnlyViolation("Refusing non-Paper trading URL")
        if not settings.alpaca_api_key or not settings.alpaca_api_secret:
            raise PaperOnlyViolation("Missing server-side Alpaca Paper credentials")

        from alpaca.data.enums import DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self._feed = DataFeed.IEX
        self._market = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_api_secret)
        self._trading = TradingClient(settings.alpaca_api_key, settings.alpaca_api_secret, paper=True)

    def market_clock(self) -> dict[str, Any]:
        clock = self._trading.get_clock()
        return {
            "is_open": bool(clock.is_open),
            "next_open": clock.next_open.isoformat() if clock.next_open else None,
            "next_close": clock.next_close.isoformat() if clock.next_close else None,
        }

    def account_snapshot(self) -> dict[str, Any]:
        account = self._trading.get_account()
        return {
            "status": str(account.status),
            "cash": float(account.cash),
            "equity": float(account.equity),
            "last_equity": float(account.last_equity),
            "buying_power": float(account.buying_power),
            "portfolio_value": float(account.portfolio_value),
            "currency": str(account.currency),
            "pattern_day_trader": bool(account.pattern_day_trader),
            "trading_blocked": bool(account.trading_blocked),
        }

    def positions(self) -> list[dict[str, Any]]:
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "side": str(p.side),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
            }
            for p in self._trading.get_all_positions()
        ]

    def latest_bars(self, symbols: list[str], lookback_minutes: int = 60 * 24 * 5) -> pd.DataFrame:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        from_time = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
        request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=from_time,
            limit=10_000,
            feed=self._feed,
        )
        bars = self._market.get_stock_bars(request).df
        if bars.empty:
            return pd.DataFrame(columns=["timestamp", "symbol", "open", "high", "low", "close", "volume"])
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.reset_index()
        else:
            bars = bars.reset_index().rename(columns={"index": "timestamp"})
        bars.columns = [str(c).lower() for c in bars.columns]
        if "symbol" not in bars.columns:
            if len(symbols) != 1:
                raise ValueError("Alpaca bar response omitted symbol for a multi-symbol request")
            bars["symbol"] = symbols[0]
        bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
        return bars[["timestamp", "symbol", "open", "high", "low", "close", "volume"]].sort_values(
            ["symbol", "timestamp"], kind="stable"
        ).reset_index(drop=True)

    def open_orders(self) -> list[dict[str, Any]]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self._trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
        return [
            {"id": str(o.id), "symbol": o.symbol, "side": str(o.side), "qty": float(o.qty or 0),
             "status": str(o.status), "client_order_id": str(o.client_order_id)}
            for o in orders
        ]

    def submit_market_order(self, symbol: str, qty: int, side: str, client_order_id: str) -> dict[str, Any]:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if qty <= 0 or side not in {"buy", "sell"}:
            raise ValueError("market order requires positive whole-share qty and side buy/sell")
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        order = self._trading.submit_order(
            MarketOrderRequest(
                symbol=symbol.upper(),
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                client_order_id=client_order_id[:48],
            )
        )
        return {
            "id": str(order.id),
            "client_order_id": str(order.client_order_id),
            "symbol": order.symbol,
            "qty": float(order.qty or 0),
            "side": str(order.side),
            "status": str(order.status),
            "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
        }
