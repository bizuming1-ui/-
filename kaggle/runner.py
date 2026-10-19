# Kaggle runner appended to shared/quant_core.py by scripts/build_kaggle_artifacts.py.
# Set SOURCE_TIMEZONE to the timezone documented for the attached CSV before relying
# on session filtering. The selected Kaggle data card does not clearly document it.

from pathlib import Path
import json
import os
import warnings

import matplotlib.pyplot as plt

DATA_ROOT = Path(os.environ.get("QUANT_DATA_ROOT", "/kaggle/input"))
OUTPUT_ROOT = Path(os.environ.get("QUANT_OUTPUT_ROOT", "/kaggle/working"))
WATCHLIST = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"]
SOURCE_TIMEZONE = None  # Set only after verifying the dataset's documented timestamp timezone.
FAST_WINDOW = 20
SLOW_WINDOW = 50
INITIAL_CASH = 100_000.0
PER_SYMBOL_FRACTION = 0.10
MAX_GROSS_FRACTION = 0.50
SLIPPAGE_BPS_PER_SIDE = 5.0
COMMISSION_PER_ORDER = 0.0
TRAIN_FRACTION = 0.70


def _is_m5_path(path: Path) -> bool:
    text = str(path).lower().replace("_", "-").replace(" ", "-")
    markers = ("m5", "5m", "5-min", "5minute", "5-minute")
    return any(marker in text for marker in markers)


def discover_symbol_files(root: Path, symbols: list[str]) -> dict[str, Path]:
    """Find one M5 CSV per requested symbol without loading the full dataset."""
    found: dict[str, Path] = {}
    if not root.exists():
        return found
    paths = sorted(root.rglob("*.csv"))
    for symbol in symbols:
        token = symbol.lower()
        candidates = [p for p in paths if token in str(p).lower() and _is_m5_path(p)]
        if candidates:
            # Prefer a filename that includes an explicit M5 marker, then the shortest path.
            candidates.sort(key=lambda p: (not _is_m5_path(Path(p.name)), len(str(p))))
            found[symbol] = candidates[0]
    return found


def load_selected_bars() -> tuple[pd.DataFrame, list[str]]:
    files = discover_symbol_files(DATA_ROOT, WATCHLIST)
    missing = sorted(set(WATCHLIST) - set(files))
    frames = []
    for symbol, path in files.items():
        print(f"读取 {symbol}: {path}")
        raw = pd.read_csv(path)
        bars = normalize_ohlcv(
            raw,
            source_timezone=SOURCE_TIMEZONE,
            default_symbol=symbol,
            regular_hours_only=True,
        )
        bars["symbol"] = symbol
        frames.append(bars)
    if missing:
        print("未找到以下标的的 M5 文件（已跳过，不会填造数据）:", ", ".join(missing))
    if not frames:
        raise FileNotFoundError(
            "未在 /kaggle/input 找到观察清单对应的 M5 CSV。请添加 USA 514 数据集，"
            "确认文件名/目录包含 ticker 与 M5/5m。"
        )
    return pd.concat(frames, ignore_index=True).sort_values(["timestamp", "symbol"]).reset_index(drop=True), missing


def _serializable_metrics(result: dict) -> dict:
    return {
        "metrics": result["metrics"],
        "benchmark_metrics": result["benchmark_metrics"],
        "symbols": result["symbols"],
        "final_cash": result["final_cash"],
        "open_positions": result["open_positions"],
        "config": result["config"],
    }


def main() -> None:
    print("Quant Desk · Kaggle CPU Research")
    print("模拟/历史研究，不连接券商；不构成投资建议。")
    print(f"窗口 SMA {FAST_WINDOW}/{SLOW_WINDOW} · 5min · RTH · 滑点 {SLIPPAGE_BPS_PER_SIDE:g} bps/边")
    print(f"源时区配置: {SOURCE_TIMEZONE}（请先根据数据卡/样例确认；若不符请修改配置）")

    bars, missing = load_selected_bars()
    start = bars["timestamp"].min().tz_convert(NY_TZ)
    end = bars["timestamp"].max().tz_convert(NY_TZ)
    print(f"实际输入: {len(bars):,} bars · {bars['symbol'].nunique()} symbols · {start} → {end}")
    if bars["symbol"].nunique() < 2:
        warnings.warn("当前只有一个标的可用于回测；组合结果不代表多资产组合。")

    config = BacktestConfig(
        fast_window=FAST_WINDOW,
        slow_window=SLOW_WINDOW,
        initial_cash=INITIAL_CASH,
        per_symbol_equity_fraction=PER_SYMBOL_FRACTION,
        max_gross_exposure_fraction=MAX_GROSS_FRACTION,
        slippage_bps=SLIPPAGE_BPS_PER_SIDE,
        commission_per_order=COMMISSION_PER_ORDER,
    )
    train, validation = split_by_time(bars, TRAIN_FRACTION)
    train_result = run_backtest(train, config)
    validation_result = run_backtest(validation, config)

    print("\n开发/观察区间（前 70%，不用于声称未来表现）")
    print(json.dumps(_serializable_metrics(train_result), indent=2, default=str))
    print("\n留出验证区间（后 30%，每段独立重新初始化账户；均线 warm-up 仅使用该段历史）")
    print(json.dumps(_serializable_metrics(validation_result), indent=2, default=str))

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "data_start_utc": bars["timestamp"].min().isoformat(),
        "data_end_utc": bars["timestamp"].max().isoformat(),
        "timezone_config": SOURCE_TIMEZONE,
        "missing_symbols": missing,
        "train": _serializable_metrics(train_result),
        "validation": _serializable_metrics(validation_result),
        "disclaimer": "Historical research output; not live performance or investment advice.",
    }
    (OUTPUT_ROOT / "quant_results.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    validation_result["equity_curve"].to_csv(OUTPUT_ROOT / "validation_equity.csv", index=False)
    validation_result["trades"].to_csv(OUTPUT_ROOT / "validation_trades.csv", index=False)

    fig, ax = plt.subplots(figsize=(12, 4.6), facecolor="#101719")
    ax.set_facecolor("#101719")
    curve = validation_result["equity_curve"]
    benchmark = validation_result["benchmark_curve"]
    ax.plot(curve["timestamp"], curve["equity"], color="#b5f37c", linewidth=1.6, label="SMA 20/50")
    if not benchmark.empty:
        ax.plot(benchmark["timestamp"], benchmark["equity"], color="#8994ec", linewidth=1.2, linestyle="--", label="Buy & Hold")
    ax.set_title("Chronological holdout · 30%", color="#e9eeea", loc="left")
    ax.tick_params(colors="#82908a", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#2b3636")
    ax.grid(axis="y", color="#ffffff", alpha=0.07)
    ax.legend(frameon=False, labelcolor="#aab6ae", fontsize=8)
    fig.tight_layout()
    fig.savefig(OUTPUT_ROOT / "validation_equity.png", dpi=140, facecolor=fig.get_facecolor())
    plt.show()
    print(f"导出完成: {OUTPUT_ROOT}/quant_results.json, validation_equity.csv, validation_trades.csv")


if __name__ == "__main__":
    main()
