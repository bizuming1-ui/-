from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StateStore:
    """Small SQLite store for idempotency, restart watermarks, and engine state."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS engine_state (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '已暂停',
                    paused_reason TEXT,
                    updated_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO engine_state(id, enabled, note, updated_at)
                    VALUES(1, 0, '已暂停', CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS watermarks (
                    symbol TEXT PRIMARY KEY,
                    bar_ts TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS processed_bars (
                    symbol TEXT NOT NULL,
                    bar_ts TEXT NOT NULL,
                    signal TEXT NOT NULL,
                    state TEXT NOT NULL,
                    client_order_id TEXT,
                    message TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, bar_ts)
                );
                CREATE INDEX IF NOT EXISTS idx_processed_created ON processed_bars(created_at);
                CREATE TABLE IF NOT EXISTS daily_baselines (
                    session_date TEXT PRIMARY KEY,
                    equity REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    def engine_state(self) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as db:
            row = db.execute("SELECT enabled,note,paused_reason,updated_at FROM engine_state WHERE id=1").fetchone()
            return dict(row)

    def set_engine(self, enabled: bool, note: str, paused_reason: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db:
            db.execute(
                "UPDATE engine_state SET enabled=?,note=?,paused_reason=?,updated_at=? WHERE id=1",
                (int(enabled), note[:240], paused_reason, now),
            )

    def watermark(self, symbol: str) -> str | None:
        with self._lock, closing(self._connect()) as db:
            row = db.execute("SELECT bar_ts FROM watermarks WHERE symbol=?", (symbol.upper(),)).fetchone()
            return str(row[0]) if row else None

    def set_watermark(self, symbol: str, bar_ts: str) -> None:
        with self._lock, closing(self._connect()) as db:
            db.execute(
                "INSERT INTO watermarks(symbol,bar_ts) VALUES(?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET bar_ts=excluded.bar_ts",
                (symbol.upper(), bar_ts),
            )

    def claim_bar(self, symbol: str, bar_ts: str, signal: str, client_order_id: str | None = None) -> bool:
        """Atomically mark a bar before any external side effect; duplicate claims fail closed."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO processed_bars(symbol,bar_ts,signal,state,client_order_id,created_at) "
                "VALUES(?,?,?,'processing',?,?)",
                (symbol.upper(), bar_ts, signal, client_order_id, now),
            )
            return cursor.rowcount == 1

    def finish_bar(self, symbol: str, bar_ts: str, state: str, message: str = "") -> None:
        with self._lock, closing(self._connect()) as db:
            db.execute(
                "UPDATE processed_bars SET state=?,message=? WHERE symbol=? AND bar_ts=?",
                (state[:40], message[:240], symbol.upper(), bar_ts),
            )

    def daily_baseline(self, session_date: str, proposed_equity: float) -> float:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db:
            db.execute(
                "INSERT OR IGNORE INTO daily_baselines(session_date,equity,created_at) VALUES(?,?,?)",
                (session_date, float(proposed_equity), now),
            )
            row = db.execute("SELECT equity FROM daily_baselines WHERE session_date=?", (session_date,)).fetchone()
            return float(row[0])

    def reset_daily_baseline(self, session_date: str, equity: float) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db:
            db.execute(
                "INSERT INTO daily_baselines(session_date,equity,created_at) VALUES(?,?,?) "
                "ON CONFLICT(session_date) DO UPDATE SET equity=excluded.equity,created_at=excluded.created_at",
                (session_date, float(equity), now),
            )

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as db:
            rows = db.execute(
                "SELECT symbol,bar_ts,signal,state,client_order_id,message,created_at "
                "FROM processed_bars ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
            return [dict(row) for row in rows]
