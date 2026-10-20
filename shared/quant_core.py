"""Shared, CPU-friendly signal and backtest core.

All timestamps entering this module must be timezone-aware. Signals are calculated
from completed bars only; backtest orders fill at the next available bar's open.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import floor
from typing import Any

import numpy as np
import pandas as pd

NY_TZ = "America/New_York"
SZ_TZ = "Asia/Shanghai"
REQUIRED_OHLCV = ("open", "high", "low", "close", "volume")
# 159502 深圳市场：09:30-11:30, 13:00-15:00 CST；成本为占位值，非真实费率
SZ_SESSION = ((9, 30, 11, 30), (13, 0, 15, 0))
ALIASES = {
    "timestamp": ("timestamp", "datetime", "date", "time", "DateTime"),
    "open": ("open", "Open"),
    "high": ("high", "High"),
    "low": ("low", "Low"),
    "close": ("close", "Close", "adj close", "Adj Close"),
    "volume": ("volume", "Volume"),
    "symbol": ("symbol", "ticker", "Ticker", "Symbol"),
}


@dataclass(frozen=True)
class BacktestConfig:
    fast_window: int = 20
    slow_window: int = 50
    initial_cash: float = 100_000.0
    per_symbol_equity_fraction: float = 0.10
    max_gross_exposure_fraction: float = 0.50
    slippage_bps: float = 5.0
    commission_per_order: float = 0.0
    bars_per_year: int = 252  # Metrics are calculated from daily marked equity, not raw intraday bars.
    # 159502 扩展：市场与货币仅用于标注，成本为占位值
    market: str = "us"
    currency: str = "USD"
    cost_note: str = "成本为可配置占位值，非真实费率"

    def validate(self) -> None:
        if not (1 <= self.fast_window < self.slow_window):
            raise ValueError("fast_window must be positive and less than slow_window")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.bars_per_year <= 0:
            raise ValueError("bars_per_year must be positive")
        if not (0 < self.per_symbol_equity_fraction <= 1):
            raise ValueError("per_symbol_equity_fraction must be in (0, 1]")
        if not (0 < self.max_gross_exposure_fraction <= 1):
            raise ValueError("max_gross_exposure_fraction must be in (0, 1]")
        if not (0 <= self.slippage_bps < 10_000) or self.commission_per_order < 0:
            raise ValueError("slippage must be in [0, 10000) bps and commission must be non-negative")
        if self.market not in ("us", "sz"):
            raise ValueError("market must be 'us' or 'sz'")
        if self.currency not in ("USD", "CNY"):
            raise ValueError("currency must be USD or CNY")


def _find_column(columns: list[Any], names: tuple[str, ...]) -> Any | None:
    lowered = {str(c).strip().lower(): c for c in columns}
    for name in names:
        found = lowered.get(name.strip().lower())
        if found is not None:
            return found
    return None


def normalize_ohlcv(
    frame: pd.DataFrame,
    *,
    source_timezone: str | None = None,
    default_symbol: str | None = None,
    regular_hours_only: bool = True,
    market: str = "us",
    session_timezone: str | None = None,
) -> pd.DataFrame:
    """Normalize common CSV schemas to UTC OHLCV rows.

    Naive timestamps are rejected unless the caller explicitly supplies the source
    timezone. This prevents silently shifting bars across US market sessions/DST.
    """
    if frame.empty:
        raise ValueError("CSV contains no data rows")
    columns = list(frame.columns)
    timestamp_col = _find_column(columns, ALIASES["timestamp"])
    if timestamp_col is None:
        raise ValueError("missing timestamp column (timestamp/datetime/date/time)")

    selected: dict[str, Any] = {}
    for field in REQUIRED_OHLCV:
        column = _find_column(columns, ALIASES[field])
        if column is None:
            raise ValueError(f"missing required OHLCV column: {field}")
        selected[field] = column
    symbol_col = _find_column(columns, ALIASES["symbol"])

    out = pd.DataFrame(index=frame.index)
    raw_values = frame[timestamp_col]
    valid_values = raw_values.notna()
    text_values = raw_values.astype(str).str.strip()
    has_zone = text_values.str.contains(r"(?:[zZ]|[+-]\d{2}:?\d{2})$", regex=True)
    zone_flags = has_zone.loc[valid_values]
    if zone_flags.any() and not zone_flags.all():
        raise ValueError("timestamp column mixes naive and timezone-aware values")
    if zone_flags.any():
        # Parsing with utc=True handles DST offsets without pandas mixed-zone warnings.
        timestamps_utc = pd.to_datetime(raw_values, errors="coerce", utc=True)
    else:
        raw_ts = pd.to_datetime(raw_values, errors="coerce")
        if not isinstance(raw_ts.dtype, pd.DatetimeTZDtype):
            if not source_timezone:
                raise ValueError("naive timestamps require an explicit source_timezone")
            try:
                timestamps_utc = raw_ts.dt.tz_localize(
                    source_timezone, ambiguous="raise", nonexistent="raise"
                ).dt.tz_convert("UTC")
            except Exception as exc:
                raise ValueError(f"cannot localize timestamps using {source_timezone}: {exc}") from exc
        else:
            timestamps_utc = raw_ts.dt.tz_convert("UTC")
    if timestamps_utc.isna().any():
        raise ValueError("timestamp column contains invalid values")
    out["timestamp"] = timestamps_utc

    for field, col in selected.items():
        out[field] = pd.to_numeric(frame[col], errors="coerce").astype("float64")
    if symbol_col is not None:
        out["symbol"] = frame[symbol_col].astype(str).str.upper().str.strip()
    elif default_symbol:
        out["symbol"] = default_symbol.upper().strip()
    else:
        out["symbol"] = "UNKNOWN"

    numeric = out[list(REQUIRED_OHLCV)].to_numpy()
    if not np.isfinite(numeric).all():
        raise ValueError("OHLCV contains missing or non-finite values")
    if (out[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("prices must be positive")
    if (out["volume"] < 0).any():
        raise ValueError("volume must be non-negative")
    if (out["high"] < out[["open", "close", "low"]].max(axis=1)).any():
        raise ValueError("high is below open, close, or low")
    if (out["low"] > out[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("low is above open, close, or high")
    if out["symbol"].eq("").any():
        raise ValueError("symbol cannot be blank")

    out = out.sort_values(["symbol", "timestamp"], kind="stable").reset_index(drop=True)
    if out.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("duplicate symbol/timestamp rows found")
    if regular_hours_only:
        tz = session_timezone or (SZ_TZ if market == "sz" else NY_TZ)
        local = out["timestamp"].dt.tz_convert(tz)
        minutes = local.dt.hour * 60 + local.dt.minute
        if market == "sz":
            # 深圳 09:30-11:30, 13:00-15:00（含 09:30,13:00 开盘；15:00 为收盘不含新开盘 bar）
            # 5 分钟 bar 允许的开盘时刻：09:30-11:25 与 13:00-14:55
            morning = (minutes >= 9 * 60 + 30) & (minutes <= 11 * 60 + 25)
            afternoon = (minutes >= 13 * 60) & (minutes <= 14 * 60 + 55)
            out = out.loc[morning | afternoon].copy()
        else:
            # 美股 09:30-16:00
            out = out.loc[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)].copy()
        if out.empty:
            raise ValueError("no regular-session bars remain; verify timestamp timezone")
    return out.reset_index(drop=True)


def normalize_sz_ohlcv(
    frame: pd.DataFrame,
    *,
    source_timezone: str | None = "Asia/Shanghai",
    default_symbol: str | None = "159502.SZ",
) -> pd.DataFrame:
    """159502.SZ 专用：默认上海时区、深圳交易时段过滤、离线优先。"""
    return normalize_ohlcv(frame, source_timezone=source_timezone, default_symbol=default_symbol, regular_hours_only=True, market="sz")


def add_sma_signals(
    bars: pd.DataFrame, fast_window: int = 20, slow_window: int = 50
) -> pd.DataFrame:
    """Add rolling averages and crossover signals; no lookahead is used."""
    if fast_window <= 0 or slow_window <= fast_window:
        raise ValueError("require 0 < fast_window < slow_window")
    required = {"symbol", "timestamp", "close"}
    if not required.issubset(bars.columns):
        raise ValueError(f"bars must contain {sorted(required)}")
    result = bars.sort_values(["symbol", "timestamp"], kind="stable").copy()
    grouped = result.groupby("symbol", sort=False)["close"]
    result["sma_fast"] = grouped.transform(lambda s: s.rolling(fast_window, min_periods=fast_window).mean())
    result["sma_slow"] = grouped.transform(lambda s: s.rolling(slow_window, min_periods=slow_window).mean())
    prev_fast = result.groupby("symbol", sort=False)["sma_fast"].shift(1)
    prev_slow = result.groupby("symbol", sort=False)["sma_slow"].shift(1)
    valid = result["sma_fast"].notna() & result["sma_slow"].notna()
    result["signal"] = "HOLD"
    result.loc[valid & (prev_fast <= prev_slow) & (result["sma_fast"] > result["sma_slow"]), "signal"] = "BUY"
    result.loc[valid & (prev_fast >= prev_slow) & (result["sma_fast"] < result["sma_slow"]), "signal"] = "SELL"
    return result


def _daily_curve(curve: pd.DataFrame, market: str = "us", session_timezone: str | None = None) -> pd.DataFrame:
    """Select each session's last marked equity for daily risk metrics."""
    if curve.empty:
        return curve.copy()
    result = curve.copy()
    timestamps = pd.to_datetime(result["timestamp"], utc=True)
    tz = session_timezone or (SZ_TZ if market == "sz" else NY_TZ)
    result["_session_date"] = timestamps.dt.tz_convert(tz).dt.date
    return result.groupby("_session_date", sort=True, as_index=False).tail(1).reset_index(drop=True)


def _equity_metrics(curve: pd.DataFrame, bars_per_year: int = 252) -> dict[str, float | int | None]:
    if curve.empty:
        return {"total_return": 0.0, "annualized_return": None, "annualized_volatility": 0.0,
                "sharpe": None, "max_drawdown": 0.0, "observations": 0}
    values = curve["equity"].astype(float)
    returns = values.pct_change().dropna()
    total_return = float(values.iloc[-1] / values.iloc[0] - 1) if values.iloc[0] else 0.0
    n = len(returns)
    # CAGR is deliberately omitted for short samples; annualizing a few sessions is misleading.
    annualized_return = (
        float((values.iloc[-1] / values.iloc[0]) ** (bars_per_year / n) - 1)
        if values.iloc[0] > 0 and n >= 20 else None
    )
    daily_vol = float(returns.std(ddof=1) * np.sqrt(bars_per_year)) if n > 1 else 0.0
    sharpe = float((returns.mean() / returns.std(ddof=1)) * np.sqrt(bars_per_year)) if n > 1 and returns.std(ddof=1) > 0 else None
    drawdown = values / values.cummax() - 1
    return {
        "total_return": total_return,
        "annualized_return": annualized_return,
        "annualized_volatility": daily_vol,
        "sharpe": sharpe,
        "max_drawdown": float(drawdown.min()),
        "observations": int(len(curve)),
    }


def _round_trip_metrics(trades: list[dict[str, Any]]) -> dict[str, float | int | None]:
    entries: dict[str, dict[str, float]] = {}
    realized: list[float] = []
    for trade in trades:
        symbol = str(trade["symbol"])
        if trade["side"] == "BUY":
            entries[symbol] = {"qty": float(trade["qty"]), "price": float(trade["price"]),
                               "commission": float(trade["commission"])}
        elif trade["side"] == "SELL" and symbol in entries:
            entry = entries.pop(symbol)
            qty = min(float(trade["qty"]), entry["qty"])
            pnl = (float(trade["price"]) - entry["price"]) * qty - entry["commission"] - float(trade["commission"])
            realized.append(pnl)
    wins = [value for value in realized if value > 0]
    losses = [value for value in realized if value < 0]
    gross_loss = abs(sum(losses))
    return {
        "closed_trades": len(realized),
        "win_rate": len(wins) / len(realized) if realized else None,
        "profit_factor": sum(wins) / gross_loss if gross_loss else None,
    }


def run_backtest(bars: pd.DataFrame, config: BacktestConfig | None = None) -> dict[str, Any]:
    """Run a long-only, multi-symbol bar-by-bar simulation.

    Signals are known at a bar close and filled at the next available bar open.
    Positions are whole shares; cash, exposure, and cost limits are enforced.
    This is a simplified research model, not a broker fill simulator.
    """
    cfg = config or BacktestConfig()
    cfg.validate()
    if bars.empty:
        raise ValueError("no bars to backtest")
    required = {"timestamp", "symbol", "open", "high", "low", "close", "volume"}
    if not required.issubset(bars.columns):
        raise ValueError(f"bars must contain {sorted(required)}")
    validated = normalize_ohlcv(bars, source_timezone=None, regular_hours_only=True, market=cfg.market)
    if validated.empty:
        raise ValueError("no regular-session bars available")
    work = add_sma_signals(validated, cfg.fast_window, cfg.slow_window)
    work = work.sort_values(["timestamp", "symbol"], kind="stable").reset_index(drop=True)

    cash = float(cfg.initial_cash)
    positions: dict[str, int] = {}
    last_prices: dict[str, float] = {}
    pending: dict[str, str] = {}
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    slip = cfg.slippage_bps / 10_000.0

    for timestamp, group in work.groupby("timestamp", sort=True):
        rows = {str(row.symbol): row for row in group.itertuples(index=False)}
        open_prices = {symbol: float(row.open) for symbol, row in rows.items()}
        # Execute signals generated by a previous completed bar at this bar's open.
        for symbol, action in list(pending.items()):
            row = rows.get(symbol)
            if row is None:
                continue
            px = float(row.open)
            if action == "BUY" and positions.get(symbol, 0) == 0:
                marked_equity = cash + sum(q * open_prices.get(s, last_prices.get(s, 0.0)) for s, q in positions.items())
                exposure = sum(q * open_prices.get(s, last_prices.get(s, 0.0)) for s, q in positions.items())
                target = min(marked_equity * cfg.per_symbol_equity_fraction,
                             max(0.0, marked_equity * cfg.max_gross_exposure_fraction - exposure), cash)
                fill = px * (1 + slip)
                qty = max(0, floor(max(0.0, target - cfg.commission_per_order) / fill))
                total = qty * fill + (cfg.commission_per_order if qty else 0.0)
                if qty and total <= cash:
                    cash -= total
                    positions[symbol] = qty
                    trades.append({"timestamp": timestamp, "symbol": symbol, "side": "BUY", "qty": qty,
                                   "price": fill, "commission": cfg.commission_per_order})
            elif action == "SELL" and positions.get(symbol, 0) > 0:
                qty = positions.pop(symbol)
                fill = px * (1 - slip)
                proceeds = qty * fill - cfg.commission_per_order
                cash += max(0.0, proceeds)
                trades.append({"timestamp": timestamp, "symbol": symbol, "side": "SELL", "qty": qty,
                               "price": fill, "commission": cfg.commission_per_order})
            pending.pop(symbol, None)

        for symbol, row in rows.items():
            last_prices[symbol] = float(row.close)
            if row.signal == "BUY" and positions.get(symbol, 0) == 0:
                pending[symbol] = "BUY"
            elif row.signal == "SELL" and positions.get(symbol, 0) > 0:
                pending[symbol] = "SELL"
        equity = cash + sum(q * last_prices.get(symbol, 0.0) for symbol, q in positions.items())
        curve.append({"timestamp": timestamp, "equity": equity, "cash": cash,
                      "gross_exposure": sum(q * last_prices.get(symbol, 0.0) for symbol, q in positions.items())})

    equity_curve = pd.DataFrame(curve)
    # Daily close marks avoid treating overnight gaps as ordinary 5-minute returns.
    metrics = _equity_metrics(_daily_curve(equity_curve, market=cfg.market), cfg.bars_per_year)
    metrics.update(_round_trip_metrics(trades))
    benchmark = _buy_hold_curve(work, cfg)
    benchmark_metrics = _equity_metrics(_daily_curve(benchmark, market=cfg.market), cfg.bars_per_year)
    return {
        "config": asdict(cfg),
        "metrics": metrics,
        "benchmark_metrics": benchmark_metrics,
        "equity_curve": equity_curve,
        "benchmark_curve": benchmark,
        "trades": pd.DataFrame(trades, columns=["timestamp", "symbol", "side", "qty", "price", "commission"]),
        "final_cash": cash,
        "open_positions": positions,
        "symbols": sorted(work["symbol"].astype(str).unique().tolist()),
    }


def _buy_hold_curve(work: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    symbols = sorted(work["symbol"].astype(str).unique().tolist())
    if not symbols:
        return pd.DataFrame(columns=["timestamp", "equity"])
    investable = cfg.initial_cash * cfg.max_gross_exposure_fraction
    allocations = investable / len(symbols)
    units: dict[str, float] = {}
    prices: dict[str, float] = {}
    cash = cfg.initial_cash - investable
    slip = cfg.slippage_bps / 10_000.0
    for symbol in symbols:
        first = work.loc[work["symbol"].astype(str).eq(symbol)].iloc[0]
        fill = float(first["open"]) * (1 + slip)
        qty = floor(max(0.0, allocations - cfg.commission_per_order) / fill)
        units[symbol] = float(qty)
        purchase_cost = qty * fill + (cfg.commission_per_order if qty else 0.0)
        cash += allocations - purchase_cost
    rows = []
    for timestamp, group in work.groupby("timestamp", sort=True):
        for row in group.itertuples(index=False):
            prices[str(row.symbol)] = float(row.close)
        equity = cash + sum(units[symbol] * prices.get(symbol, 0.0) for symbol in symbols)
        rows.append({"timestamp": timestamp, "equity": equity})
    return pd.DataFrame(rows)


def split_by_time(bars: pd.DataFrame, train_fraction: float = 0.70) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Chronological split; the same timestamp cannot cross the split boundary."""
    if not 0.5 <= train_fraction < 1:
        raise ValueError("train_fraction must be in [0.5, 1)")
    times = np.array(sorted(pd.to_datetime(bars["timestamp"], utc=True).unique()))
    if len(times) < 2:
        raise ValueError("at least two unique timestamps are required")
    cut_index = min(max(1, int(len(times) * train_fraction)), len(times) - 1)
    cutoff = times[cut_index]
    timestamps = pd.to_datetime(bars["timestamp"], utc=True)
    train = bars.loc[timestamps < cutoff].copy()
    test = bars.loc[timestamps >= cutoff].copy()
    if train.empty or test.empty:
        raise ValueError("time split produced an empty partition")
    return train, test


# 159502 专用：无已授权五分钟历史时不做真实回测
def validate_sz159502_backtest_prerequisites(has_authorized_5min_history: bool) -> None:
    if not has_authorized_5min_history:
        raise ValueError(
            "159502 五分钟历史数据未获授权：历史基金接口为日线，高频分钟接口不应被假装为可用；"
            "当前仅提供离线 Demo，不做真实数据回测"
        )


def run_sz159502_backtest(
    bars: pd.DataFrame,
    config: BacktestConfig | None = None,
    *,
    has_authorized_5min_history: bool = False,
) -> dict[str, Any]:
    """159502 单标的纸面回测：强制校验 5min 授权，允许隔夜，下一根 K 开盘成交。"""
    validate_sz159502_backtest_prerequisites(has_authorized_5min_history)
    cfg = config or BacktestConfig(
        fast_window=20,
        slow_window=50,
        initial_cash=100_000.0,
        per_symbol_equity_fraction=0.10,
        max_gross_exposure_fraction=0.50,
        slippage_bps=5.0,
        commission_per_order=0.0,
        market="sz",
        currency="CNY",
        cost_note="成本为可配置占位值，非真实费率",
    )
    # 确保使用深圳时段过滤
    if bars.empty:
        raise ValueError("no bars to backtest")
    # 若调用方已做 normalize，此处复核 SZ 会话
    validated = normalize_ohlcv(bars, source_timezone=None, regular_hours_only=True, market="sz")
    return run_backtest(validated, cfg)


def calc_kdj(bars: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3) -> pd.DataFrame:
    """KDJ(9,3,3) 计算，适配照片样例；仅用已收盘 bar，无未来。"""
    need = {"high","low","close"}
    if not need.issubset(bars.columns): raise ValueError(f"kdj need {need}")
    df = bars.copy()
    low_list = df["low"].rolling(n, min_periods=n).min()
    high_list = df["high"].rolling(n, min_periods=n).max()
    rsv = (df["close"] - low_list) / (high_list - low_list) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(alpha=1/m1, adjust=False).mean()
    d = k.ewm(alpha=1/m1, adjust=False).mean()
    j = 3*k - 2*d
    df["kdj_k"], df["kdj_d"], df["kdj_j"] = k, d, j
    return df

def calc_macd(bars: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD(12,26,9) 计算，适配照片样例。"""
    if "close" not in bars.columns: raise ValueError("macd need close")
    df = bars.copy()
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    bar = (dif - dea) * 2
    df["macd_dif"], df["macd_dea"], df["macd_bar"] = dif, dea, bar
    return df

def detect_smc_structure(bars: pd.DataFrame, lookback: int = 5) -> dict[str, list[dict[str, Any]]]:
    """SMC 简化：识别 BOS/ChoCh 与支撑阻力区（基于摆动高低点）。"""
    closes = bars["close"].tolist()
    highs = bars["high"].tolist()
    lows = bars["low"].tolist()
    bos, choch, resistance, support = [], [], [], []
    last_high = max(highs[:lookback]) if len(highs)>=lookback else None
    last_low = min(lows[:lookback]) if len(lows)>=lookback else None
    for i in range(lookback, len(bars)):
        ts = bars.iloc[i]["timestamp"]
        c = closes[i]
        if last_high is not None and c > last_high:
            bos.append({"timestamp": ts, "type": "bos", "price": c})
            last_high = max(last_high, c)
        if last_low is not None and c < last_low:
            choch.append({"timestamp": ts, "type": "choch", "price": c})
            last_low = min(last_low, c)
        if highs[i] > (last_high or -1e9): last_high = highs[i]
        if lows[i] < (last_low or 1e9): last_low = lows[i]
    if bos: resistance.append({"price": max(b["price"] for b in bos[-3:]), "label": "Resistance Area"})
    if choch: support.append({"price": min(c["price"] for c in choch[-3:]), "label": "Support"})
    return {"bos": bos[-6:], "choch": choch[-4:], "resistance": resistance, "support": support}

def demo_sz159502_equity_curve(currency: str = "CNY") -> list[dict[str, Any]]:
    """离线 Demo：合成 159502 权益曲线，明确标注非实时。"""
    import math as _math
    points: list[dict[str, Any]] = []
    for i in range(48):
        # 模拟深圳 09:30-15:00 的 5 分钟 bar（剔除午休后合计 48 根）
        if i < 24:
            minutes = 9 * 60 + 30 + i * 5
        else:
            minutes = 13 * 60 + (i - 24) * 5
        p = i / 47
        wave = 220 * _math.sin(p * _math.pi * 6) * _math.sin(p * _math.pi)
        equity = 100_000 + 1_820 * p + wave
        points.append({
            "time": f"{minutes // 60:02d}:{minutes % 60:02d}",
            "equity": round(equity, 2),
            "benchmark": round(100_000 + 1_100 * p + 70 * _math.sin(p * _math.pi * 3), 2),
        })
    return points

