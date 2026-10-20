from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from backend.app.settings import Settings
from backend.app.sz_portfolio import SZPortfolioStore
from shared.quant_core import SZ_TZ

def _sz_is_trading_now(now_utc: pd.Timestamp | None = None) -> bool:
    now = now_utc or pd.Timestamp.now(tz="UTC")
    local = now.tz_convert(SZ_TZ)
    minutes = local.hour * 60 + local.minute
    # 09:30-11:30, 13:00-15:00
    return (9*60+30 <= minutes <= 11*60+30) or (13*60 <= minutes <= 15*60)

class SZPaperMarketClient:
    """对 SZ159502Engine 暴露的只做多纸面客户端：离线优先，不假装分钟级真实数据可用。"""

    def __init__(self, settings: Settings, portfolio: SZPortfolioStore, ths_client=None, demo_bars: pd.DataFrame | None = None) -> None:
        self.settings = settings
        self.portfolio = portfolio
        self.ths = ths_client
        self._demo_bars = demo_bars

    def market_clock(self) -> dict[str, Any]:
        now = pd.Timestamp.now(tz="UTC")
        is_open = _sz_is_trading_now(now)
        return {"is_open": is_open, "next_open": None, "next_close": None}

    def account_snapshot(self) -> dict[str, Any]:
        # 取最近收盘价作为当前价估算权益；离线时用最后一条 demo bar 的 close
        price = None
        if self._demo_bars is not None and not self._demo_bars.empty:
            try:
                price = float(self._demo_bars.iloc[-1]["close"])
            except Exception:
                price = None
        return self.portfolio.account_snapshot(current_price=price)

    def positions(self) -> list[dict[str, Any]]:
        price = None
        if self._demo_bars is not None and not self._demo_bars.empty:
            try:
                price = float(self._demo_bars.iloc[-1]["close"])
            except Exception:
                price = None
        return self.portfolio.positions(current_price=price)

    def open_orders(self) -> list[dict[str, Any]]:
        return self.portfolio.open_orders()

    def latest_bars(self, symbols: list[str], lookback_minutes: int = 60*24*7) -> pd.DataFrame:
        # 若有真实分钟授权且 ths 启用，则尝试调用 ths.latest_5min_bars；否则返回离线说明或合成 demo
        if self.settings.sz159502_has_authorized_5min_history and self.ths:
            try:
                result = self.ths.latest_5min_bars(self.settings.sz159502_symbol)
                bars = result.get("bars") if isinstance(result, dict) else None
                if isinstance(bars, pd.DataFrame) and not bars.empty:
                    return bars
            except Exception:
                pass
            # 即便授权，分钟接口路径待确认时仍返回占位空数据，不伪造
            return pd.DataFrame(columns=["timestamp", "symbol", "open", "high", "low", "close", "volume"])

        if self._demo_bars is not None:
            # 返回已过滤的 demo bars（已做 SZ 时段过滤）
            return self._demo_bars.copy()

        # 默认离线：返回空，引擎会判定无可用 bar 并保持暂停，不做回测
        return pd.DataFrame(columns=["timestamp", "symbol", "open", "high", "low", "close", "volume"])

    def submit_market_order(self, symbol: str, qty: int, side: str, client_order_id: str) -> dict[str, Any]:
        # 取当前 demo bar 的 open 作为下一根 K 开盘价（若无则用 close）
        price = 1.0
        if self._demo_bars is not None and not self._demo_bars.empty:
            price = float(self._demo_bars.iloc[-1].get("open", self._demo_bars.iloc[-1]["close"]))
        return self.portfolio.submit_market_order(symbol, qty, side, client_order_id, price)

    @staticmethod
    def build_demo_bars(days: int = 3, symbol: str = "159502.SZ") -> pd.DataFrame:
        """合成 159502 五分钟 Demo：仅用于界面演示，醒目标注非真实。"""
        tz = ZoneInfo(SZ_TZ)
        # 选取最近 3 个交易日的深圳交易时段
        # 为简化，直接生成 3*48=144 根 5 分钟 bar
        base = datetime.now(tz).replace(hour=9, minute=30, second=0, microsecond=0)
        # 回退到最近工作日（简化：直接生成，不校验节假日）
        bars = []
        close = 1.02
        for i in range(48 * days):
            if i < 24:
                minutes = 9 * 60 + 30 + (i % 48) * 5 if i < 24 else 13*60 + (i-24)*5
                # 实际应按天推算，此处简化为连续序列，仅演示
            # 简化时间戳：按 UTC 生成
            # 使用交错的 session 生成时间戳
            day_offset = i // 48
            idx_in_day = i % 48
            if idx_in_day < 24:
                h, m = 9, 30
                delta = idx_in_day * 5
            else:
                h, m = 13, 0
                delta = (idx_in_day - 24) * 5
            # 计算本地时间
            local = (base - timedelta(days=days-1-day_offset)).replace(hour=9, minute=30) + timedelta(minutes=delta if idx_in_day < 24 else (3*60+30)+delta - 24*5)
            # 修正：上午 9:30 开始，每 5 分钟；下午 13:00 开始
            # 上午 24 根，至 11:25；下午 24 根，至 14:55
            # 重新计算
            if idx_in_day < 24:
                local = local.replace(hour=9, minute=30) + timedelta(minutes=idx_in_day*5)
            else:
                local = local.replace(hour=13, minute=0) + timedelta(minutes=(idx_in_day-24)*5)
            ts = local.astimezone(timezone.utc)
            # 合成价格：带 SMA 交叉趋势
            # slow 50 周期内制造一次上穿与下穿
            wave = 0.04 * (1 if i % 70 < 35 else -1) * (1 + 0.1 * (i % 5))
            # 叠加小幅波动
            close = max(0.8, close + (0.001 if wave > 0 else -0.001) + ((i % 7)-3)*0.0005)
            open_p = close - 0.001
            high = max(open_p, close) + 0.003
            low = min(open_p, close) - 0.003
            volume = 800000 + (i % 10)*100000
            bars.append({"timestamp": ts, "symbol": symbol, "open": open_p, "high": high, "low": low, "close": close, "volume": volume})
        df = pd.DataFrame(bars)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
