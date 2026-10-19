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
REQUIRED_OHLCV = ("open", "high", "low", "close", "volume")
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
        local = out["timestamp"].dt.tz_convert(NY_TZ)
        minutes = local.dt.hour * 60 + local.dt.minute
        # Bar-open labels from 09:30 through 15:55 are regular session bars.
        out = out.loc[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)].copy()
        if out.empty:
            raise ValueError("no regular-session bars remain; verify timestamp timezone")
    return out.reset_index(drop=True)


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


def _daily_curve(curve: pd.DataFrame) -> pd.DataFrame:
    """Select each US session's last marked equity for daily risk metrics."""
    if curve.empty:
        return curve.copy()
    result = curve.copy()
    timestamps = pd.to_datetime(result["timestamp"], utc=True)
    result["_session_date"] = timestamps.dt.tz_convert(NY_TZ).dt.date
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
    validated = normalize_ohlcv(bars, source_timezone=None, regular_hours_only=True)
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
    metrics = _equity_metrics(_daily_curve(equity_curve), cfg.bars_per_year)
    metrics.update(_round_trip_metrics(trades))
    benchmark = _buy_hold_curve(work, cfg)
    benchmark_metrics = _equity_metrics(_daily_curve(benchmark), cfg.bars_per_year)
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
