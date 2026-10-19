from __future__ import annotations

from backend.app.store import StateStore


def test_bar_claim_is_idempotent_and_state_persists(tmp_path) -> None:
    store = StateStore(str(tmp_path / "quant.sqlite3"))
    assert store.claim_bar("AAPL", "2024-01-02T14:30:00+00:00", "BUY", "qd-aapl-test")
    assert not store.claim_bar("AAPL", "2024-01-02T14:30:00+00:00", "BUY", "qd-aapl-test")
    store.finish_bar("AAPL", "2024-01-02T14:30:00+00:00", "submitted", "order-1")
    assert store.recent_events()[0]["state"] == "submitted"
    store.set_engine(True, "resume")
    assert StateStore(str(tmp_path / "quant.sqlite3")).engine_state()["enabled"] == 1


def test_watermark_and_daily_baseline_are_stable(tmp_path) -> None:
    store = StateStore(str(tmp_path / "quant.sqlite3"))
    assert store.watermark("MSFT") is None
    store.set_watermark("MSFT", "2024-01-02T14:30:00+00:00")
    assert store.watermark("MSFT") == "2024-01-02T14:30:00+00:00"
    assert store.daily_baseline("2024-01-02", 100_000) == 100_000
    assert store.daily_baseline("2024-01-02", 98_000) == 100_000
    store.reset_daily_baseline("2024-01-02", 98_000)
    assert store.daily_baseline("2024-01-02", 100_000) == 98_000
