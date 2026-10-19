from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]


def test_bundled_kaggle_script_runs_on_small_timezone_aware_cpu_fixture(tmp_path) -> None:
    input_root = tmp_path / "input" / "usa-stocks" / "M5"
    output_root = tmp_path / "output"
    input_root.mkdir(parents=True)
    frames = []
    for day in pd.date_range("2024-01-02", periods=6, freq="B"):
        times = pd.date_range(f"{day.date()} 09:30", periods=78, freq="5min", tz="America/New_York")
        for i, timestamp in enumerate(times):
            close = 100 + 1.8 * (i / 77) + 1.3 * math.sin(i / 6 + day.day)
            frames.append({"datetime": timestamp.tz_convert("UTC").isoformat(), "open": close,
                           "high": close + .15, "low": close - .15, "close": close,
                           "volume": 1000 + i, "ticker": "AAPL.US"})
    pd.DataFrame(frames).to_csv(input_root / "AAPL.US_M5.csv", index=False)
    env = os.environ.copy()
    env.update({"QUANT_DATA_ROOT": str(tmp_path / "input"), "QUANT_OUTPUT_ROOT": str(output_root), "MPLBACKEND": "Agg"})
    result = subprocess.run([sys.executable, str(ROOT / "kaggle" / "quant_research.py")],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=45, check=False)
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "实际输入: 468 bars · 1 symbols" in result.stdout
    report = json.loads((output_root / "quant_results.json").read_text())
    assert report["train"]["symbols"] == ["AAPL"]
    assert report["missing_symbols"] == ["MSFT", "NVDA", "QQQ", "SPY"]
    assert (output_root / "validation_equity.csv").exists()
    assert (output_root / "validation_trades.csv").exists()
    assert (output_root / "validation_equity.png").exists()
