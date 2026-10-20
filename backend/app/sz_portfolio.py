from __future__ import annotations

import math
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.app.settings import Settings

class SZPortfolioStore:
    """159502 虚拟账户：仅本地纸面，不连接实盘；初始 ¥100,000，CNY。

    持仓仅 159502.SZ，整股，允许隔夜；成本为占位值。
    """

    def __init__(self, path: str, settings: Settings) -> None:
        self.path = path
        self.settings = settings
        self._lock = threading.RLock()
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _init_db(self) -> None:
        with self._lock, closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sz_portfolio_state (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    cash REAL NOT NULL,
                    initial_equity REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sz_positions (
                    symbol TEXT PRIMARY KEY,
                    qty REAL NOT NULL,
                    avg_entry_price REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sz_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    price REAL NOT NULL,
                    status TEXT NOT NULL,
                    client_order_id TEXT,
                    created_at TEXT NOT NULL
                );
            """)
            row = db.execute("SELECT cash FROM sz_portfolio_state WHERE id=1").fetchone()
            if not row:
                db.execute(
                    "INSERT INTO sz_portfolio_state(id, cash, initial_equity, updated_at) VALUES(1,?,?,?)",
                    (float(self.settings.sz159502_initial_equity), float(self.settings.sz159502_initial_equity), datetime.now(timezone.utc).isoformat())
                )

    def account_snapshot(self, current_price: float | None = None) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as db:
            state = db.execute("SELECT cash, initial_equity FROM sz_portfolio_state WHERE id=1").fetchone()
            cash = float(state["cash"])
            initial = float(state["initial_equity"])
            pos = db.execute("SELECT qty, avg_entry_price FROM sz_positions WHERE symbol=?", (self.settings.sz159502_symbol,)).fetchone()
            qty = float(pos["qty"]) if pos else 0.0
            avg = float(pos["avg_entry_price"]) if pos else 0.0
            price = current_price if current_price is not None else avg
            market_value = qty * price if qty else 0.0
            equity = cash + market_value
            # last_equity 取上一交易日收盘权益（简化为 initial 对比，实际由 engine 的 daily_baseline 管理）
            return {
                "status": "DEMO",
                "cash": cash,
                "equity": equity,
                "last_equity": initial if equity == initial else equity,  # 占位，真实日基线由 engine 校准
                "buying_power": cash,
                "portfolio_value": market_value,
                "currency": "CNY",
                "initial_equity": initial,
                "trading_blocked": False,
            }

    def positions(self, current_price: float | None = None) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as db:
            rows = db.execute("SELECT symbol, qty, avg_entry_price FROM sz_positions").fetchall()
            out = []
            for r in rows:
                qty = float(r["qty"])
                avg = float(r["avg_entry_price"])
                price = current_price if current_price is not None else avg
                mv = qty * price
                upl = (price - avg) * qty if qty else 0.0
                out.append({
                    "symbol": r["symbol"],
                    "qty": qty,
                    "side": "long",
                    "avg_entry_price": avg,
                    "current_price": price,
                    "market_value": mv,
                    "unrealized_pl": upl,
                    "unrealized_plpc": (upl / (avg * qty) if avg * qty else 0.0),
                })
            return out

    def open_orders(self) -> list[dict[str, Any]]:
        # 159502 纸面模拟为即时成交，无挂单；保持兼容
        return []

    def submit_market_order(self, symbol: str, qty: int, side: str, client_order_id: str, price: float) -> dict[str, Any]:
        """纸面成交：下一根可交易 K 开盘价（由调用方传入 price），成本为占位值。"""
        if symbol.upper() not in (self.settings.sz159502_symbol.upper(), "159502", "159502.SZ"):
            raise ValueError(f"单标的模拟器仅接受 {self.settings.sz159502_symbol}，收到 {symbol}")
        if qty <= 0 or side not in ("buy", "sell"):
            raise ValueError("qty 必须为正整数，side 为 buy/sell")
        now = datetime.now(timezone.utc).isoformat()
        slippage = self.settings.sz159502_slippage_bps / 10_000.0
        commission = self.settings.sz159502_commission_per_order
        with self._lock, closing(self._connect()) as db:
            state = db.execute("SELECT cash FROM sz_portfolio_state WHERE id=1").fetchone()
            cash = float(state["cash"])
            pos = db.execute("SELECT qty, avg_entry_price FROM sz_positions WHERE symbol=?", (symbol.upper(),)).fetchone()
            cur_qty = float(pos["qty"]) if pos else 0.0
            cur_avg = float(pos["avg_entry_price"]) if pos else 0.0
            if side == "buy":
                fill = price * (1 + slippage)
                cost = qty * fill + commission
                if cost > cash + 1e-9:
                    raise ValueError(f"现金不足：需 {cost:.2f}，可用 {cash:.2f}（成本为占位值）")
                new_cash = cash - cost
                new_qty = cur_qty + qty
                # 加权平均成本（含滑点影响）
                new_avg = (cur_qty * cur_avg + qty * fill) / new_qty if new_qty else 0.0
                db.execute("UPDATE sz_portfolio_state SET cash=?, updated_at=? WHERE id=1", (new_cash, now))
                db.execute(
                    "INSERT INTO sz_positions(symbol, qty, avg_entry_price, updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty, avg_entry_price=excluded.avg_entry_price, updated_at=excluded.updated_at",
                    (symbol.upper(), new_qty, new_avg, now)
                )
            else:  # sell
                if cur_qty < qty - 1e-9:
                    raise ValueError(f"持仓不足：持有 {cur_qty}，尝试卖出 {qty}")
                fill = price * (1 - slippage)
                proceeds = qty * fill - commission
                new_cash = cash + proceeds
                new_qty = cur_qty - qty
                db.execute("UPDATE sz_portfolio_state SET cash=?, updated_at=? WHERE id=1", (new_cash, now))
                if new_qty <= 0.5:
                    db.execute("DELETE FROM sz_positions WHERE symbol=?", (symbol.upper(),))
                else:
                    # 卖出不改变剩余持仓成本
                    db.execute("UPDATE sz_positions SET qty=?, updated_at=? WHERE symbol=?", (new_qty, now, symbol.upper()))
            order_id = f"sz-{int(datetime.now(timezone.utc).timestamp()*1000)}-{qty}{side[0]}"
            db.execute(
                "INSERT INTO sz_orders(symbol, side, qty, price, status, client_order_id, created_at) VALUES(?,?,?,?,?,?,?)",
                (symbol.upper(), side, qty, price, "FILLED", client_order_id, now)
            )
            return {"id": order_id, "client_order_id": client_order_id, "symbol": symbol.upper(), "qty": float(qty), "side": side, "status": "FILLED", "submitted_at": now}

    def reset_for_test(self, cash: float = 100_000.0) -> None:
        with self._lock, closing(self._connect()) as db:
            now = datetime.now(timezone.utc).isoformat()
            db.execute("UPDATE sz_portfolio_state SET cash=?, initial_equity=?, updated_at=? WHERE id=1", (cash, cash, now))
            db.execute("DELETE FROM sz_positions")
            db.execute("DELETE FROM sz_orders")

    def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as db:
            rows = db.execute("SELECT symbol, side, qty, price, status, client_order_id, created_at FROM sz_orders ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
