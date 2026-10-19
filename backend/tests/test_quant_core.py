from __future__ import annotations

import pandas as pd
import pytest

from shared.quant_core import BacktestConfig, add_sma_signals, normalize_ohlcv, run_backtest, split_by_time


def make_bars(closes: list[float], opens: list[float] | None = None, symbol: str = "AAPL") -> pd.DataFrame:
    opens = opens or closes
    stamps = pd.date_range("2024-01-02 09:30", periods=len(closes), freq="5min", tz="America/New_York").tz_convert("UTC")
    return pd.DataFrame({
        "timestamp": stamps, "symbol": symbol, "open": opens,
        "high": [max(o, c) + 0.1 for o, c in zip(opens, closes)],
        "low": [min(o, c) - 0.1 for o, c in zip(opens, closes)],
        "close": closes, "volume": [1000] * len(closes),
    })


def test_naive_timestamp_requires_timezone() -> None:
    frame = pd.DataFrame({
        "datetime": ["2024-01-02 09:30"], "open": [10], "high": [11],
        "low": [9], "close": [10], "volume": [1], "ticker": ["AAPL"],
    })
    with pytest.raises(ValueError, match="explicit source_timezone"):
        normalize_ohlcv(frame)


def test_mixed_dst_offsets_normalize_to_utc_without_shifting_local_session() -> None:
    frame = pd.DataFrame({
        "datetime": ["2024-01-02T09:30:00-05:00", "2024-07-02T09:30:00-04:00"],
        "open": [10, 11], "high": [11, 12], "low": [9, 10], "close": [10, 11],
        "volume": [10, 10], "symbol": ["AAPL", "AAPL"],
    })
    out = normalize_ohlcv(frame)
    assert out["timestamp"].tolist() == [pd.Timestamp("2024-01-02 14:30", tz="UTC"), pd.Timestamp("2024-07-02 13:30", tz="UTC")]


def test_normalize_local_time_to_utc_and_filter_rth() -> None:
    frame = pd.DataFrame({
        "datetime": ["2024-01-02 09:25", "2024-01-02 09:30", "2024-01-02 15:55", "2024-01-02 16:00"],
        "Open": [10] * 4, "High": [11] * 4, "Low": [9] * 4, "Close": [10] * 4,
        "Volume": [100] * 4, "Ticker": ["aapl"] * 4,
    })
    out = normalize_ohlcv(frame, source_timezone="America/New_York")
    assert len(out) == 2
    assert out["symbol"].tolist() == ["AAPL", "AAPL"]
    assert out.iloc[0]["timestamp"] == pd.Timestamp("2024-01-02 14:30", tz="UTC")


def test_bad_ohlc_rejected() -> None:
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-02 14:30", periods=1, tz="UTC"),
        "open": [10], "high": [9], "low": [8], "close": [10], "volume": [1], "symbol": ["AAPL"],
    })
    with pytest.raises(ValueError, match="high is below"):
        normalize_ohlcv(frame)


def test_signals_use_only_current_and_past_bars() -> None:
    bars = make_bars([10, 9, 8, 7, 8, 10, 11, 8, 6])
    result = add_sma_signals(bars, fast_window=2, slow_window=3)
    assert result.loc[5, "signal"] == "BUY"
    assert result.loc[7, "signal"] == "SELL"
    changed = bars.copy()
    changed.loc[8, "close"] = 1000
    changed_signals = add_sma_signals(changed, fast_window=2, slow_window=3)
    assert result.loc[:7, "signal"].tolist() == changed_signals.loc[:7, "signal"].tolist()


def test_backtest_fills_after_signal_bar_and_applies_slippage() -> None:
    bars = make_bars([10, 9, 8, 7, 8, 10, 11, 8, 6], [10, 9, 8, 7, 8, 10, 12, 8, 7])
    cfg = BacktestConfig(fast_window=2, slow_window=3, initial_cash=10_000,
                         per_symbol_equity_fraction=0.2, max_gross_exposure_fraction=0.5,
                         slippage_bps=5, commission_per_order=0)
    result = run_backtest(bars, cfg)
    trades = result["trades"]
    assert trades["side"].tolist() == ["BUY", "SELL"]
    assert trades.iloc[0]["timestamp"] == bars.iloc[6]["timestamp"]
    assert trades.iloc[1]["timestamp"] == bars.iloc[8]["timestamp"]
    assert trades.iloc[0]["price"] == pytest.approx(12 * 1.0005)
    assert trades.iloc[1]["price"] == pytest.approx(7 * 0.9995)
    assert result["metrics"]["closed_trades"] == 1
    assert result["metrics"]["win_rate"] == 0.0
    assert result["metrics"]["observations"] == 1  # Daily marked metrics, not 9 intraday bars.


def test_simultaneous_multi_symbol_entries_respect_gross_exposure_cap() -> None:
    signals = make_bars([10, 9, 8, 7, 8, 10, 11], symbol="AAPL")
    bars = pd.concat([signals.assign(symbol=symbol) for symbol in ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"]], ignore_index=True)
    result = run_backtest(bars, BacktestConfig(fast_window=2, slow_window=3, initial_cash=10_000,
                                               per_symbol_equity_fraction=.10,
                                               max_gross_exposure_fraction=.50, slippage_bps=5))
    assert result["equity_curve"]["gross_exposure"].max() <= 5_000
    assert len(result["open_positions"]) == 5


def test_buy_and_hold_baseline_respects_same_50_percent_exposure_cap() -> None:
    bars = make_bars([10, 10, 10, 10, 20, 20, 20])
    result = run_backtest(bars, BacktestConfig(fast_window=2, slow_window=3, initial_cash=10_000,
                                               max_gross_exposure_fraction=.50, slippage_bps=0))
    baseline_final = float(result["benchmark_curve"]["equity"].iloc[-1])
    assert 14_900 <= baseline_final <= 15_100


def test_time_split_is_chronological_and_disjoint() -> None:
    bars = make_bars(list(range(10, 30)))
    train, test = split_by_time(bars, 0.7)
    assert len(train) == 14
    assert len(test) == 6
    assert train["timestamp"].max() < test["timestamp"].min()


def test_config_rejects_bad_window() -> None:
    with pytest.raises(ValueError, match="fast_window"):
        BacktestConfig(fast_window=50, slow_window=20).validate()
