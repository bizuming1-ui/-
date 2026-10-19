from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from backend.app.alpaca_paper import AlpacaPaperClient
from backend.app.settings import Settings
from backend.app.store import StateStore
from shared.quant_core import NY_TZ, add_sma_signals, normalize_ohlcv

logger = logging.getLogger("quantdesk.engine")


class EnginePreflightError(RuntimeError):
    pass


class PaperEngine:
    """Fail-closed, single-process Paper-only order loop.

    Data is polled in one batched request, decisions use completed regular-session
    bars only, and each symbol/bar is atomically claimed before external submission.
    """

    def __init__(self, settings: Settings, client: AlpacaPaperClient, store: StateStore) -> None:
        self.settings = settings
        self.client = client
        self.store = store
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._note = "已暂停"
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.store.engine_state()["enabled"])

    @property
    def note(self) -> str:
        return str(self.store.engine_state()["note"])

    async def start(self, *, after_restart: bool = False, acknowledge_risk: bool = False) -> dict[str, Any]:
        async with self._lock:
            if self.enabled and self._task and not self._task.done():
                return {"enabled": True, "note": self.note}
            try:
                prior_state = self.store.engine_state()
                if not after_restart and prior_state.get("paused_reason") == "daily_loss_limit":
                    if not acknowledge_risk:
                        raise EnginePreflightError("当日回撤熔断已触发；需明确确认风险后才能重新启用")
                    account_before_reset = await asyncio.to_thread(self.client.account_snapshot)
                    day = datetime.now(timezone.utc).astimezone(ZoneInfo(NY_TZ)).date().isoformat()
                    self.store.reset_daily_baseline(day, float(account_before_reset["equity"]))
                await self._reconcile_account()
                # On every start/restart, set watermarks to the current latest complete
                # bar. Old bars/signals are deliberately skipped; the next new bar is eligible.
                await self._prime_watermarks()
                if not after_restart and acknowledge_risk:
                    # Explicit user confirmation permits a re-arm. The next polling cycle
                    # re-evaluates the preserved daily baseline; it may immediately re-trip.
                    pass
                self.store.set_engine(True, "已启用 Paper 策略；等待下一根完整 5 分钟 bar", None)
                self._stop = asyncio.Event()
                self._task = asyncio.create_task(self._run(), name="quant-paper-engine")
                return {"enabled": True, "note": self.note}
            except Exception as exc:
                self.store.set_engine(False, "Paper 账户预检失败；策略保持暂停", "preflight")
                if isinstance(exc, EnginePreflightError):
                    raise
                logger.exception("Paper engine preflight failed")
                raise EnginePreflightError("Paper 账户预检失败；检查 Paper 账户、行情订阅和网络") from exc

    async def resume_after_restart(self) -> dict[str, Any]:
        state = self.store.engine_state()
        if not state["enabled"]:
            return {"enabled": False, "note": state["note"]}
        if not self.settings.auto_resume:
            self.store.set_engine(False, "服务重启后按配置保持暂停", "auto_resume_disabled")
            return {"enabled": False, "note": self.note}
        # A daily-loss latch or prior safety error must never be automatically cleared.
        if state.get("paused_reason"):
            self.store.set_engine(False, "先前触发安全暂停；需人工检查后恢复", state["paused_reason"])
            return {"enabled": False, "note": self.note}
        try:
            return await self.start(after_restart=True)
        except Exception:
            logger.exception("Automatic Paper resume failed; engine remains paused")
            return {"enabled": False, "note": self.note}

    async def stop(self, note: str = "已暂停新订单") -> dict[str, Any]:
        async with self._lock:
            self.store.set_engine(False, note, None)
            self._stop.set()
            task = self._task
            self._task = None
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            return {"enabled": False, "note": note}

    async def _reconcile_account(self) -> None:
        account = await asyncio.to_thread(self.client.account_snapshot)
        if account.get("trading_blocked"):
            raise EnginePreflightError("Paper 账户当前被券商禁止交易")
        if float(account.get("equity", 0)) <= 0 or float(account.get("cash", 0)) < 0:
            raise EnginePreflightError("Paper 账户权益/现金异常")
        orders = await asyncio.to_thread(self.client.open_orders)
        if orders:
            raise EnginePreflightError("Paper 账户存在未完成订单；先在 Alpaca Paper 中检查并处理")
        positions = await asyncio.to_thread(self.client.positions)
        allowed = set(self.settings.strategy_symbols)
        for position in positions:
            symbol = str(position["symbol"]).upper()
            side = str(position.get("side", "long")).lower()
            qty = float(position.get("qty", 0))
            if symbol not in allowed:
                raise EnginePreflightError(f"发现不在策略观察清单内的持仓 {symbol}；先人工对账")
            if "short" in side or qty < 0:
                raise EnginePreflightError(f"发现空头仓位 {symbol}；本策略只允许做多")
            if not math.isclose(qty, round(qty), abs_tol=1e-8):
                raise EnginePreflightError(f"发现非整股仓位 {symbol}；请先人工对账")

    async def _prime_watermarks(self) -> None:
        bars = await asyncio.to_thread(self.client.latest_bars, self.settings.strategy_symbols, 60 * 24 * 7)
        if bars.empty:
            raise EnginePreflightError("Alpaca 没有返回可用的历史 bar；策略不会启动")
        normalized = normalize_ohlcv(bars, regular_hours_only=True)
        now = pd.Timestamp.now(tz="UTC")
        normalized = normalized.loc[normalized["timestamp"] + pd.Timedelta(minutes=5) <= now]
        if normalized.empty:
            raise EnginePreflightError("没有已完成的常规时段 bar；策略不会启动")
        for symbol, group in normalized.groupby("symbol"):
            latest = group["timestamp"].max().isoformat()
            self.store.set_watermark(str(symbol), latest)

    async def _run(self) -> None:
        logger.info("Paper engine loop started")
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Paper engine encountered an error; fail closed")
                self.store.set_engine(False, "行情/账户处理异常，已暂停新订单", "engine_error")
                self._note = type(exc).__name__
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.poll_seconds)
            except asyncio.TimeoutError:
                pass
        logger.info("Paper engine loop stopped")

    async def _poll_once(self) -> None:
        bars = await asyncio.to_thread(self.client.latest_bars, self.settings.strategy_symbols, 60 * 24 * 7)
        if bars.empty:
            raise EnginePreflightError("行情返回空数据；策略已暂停")
        normalized = normalize_ohlcv(bars, regular_hours_only=True)
        now = pd.Timestamp.now(tz="UTC")
        normalized = normalized.loc[normalized["timestamp"] + pd.Timedelta(minutes=5) <= now]
        if normalized.empty:
            return

        market_clock = getattr(self.client, "market_clock", None)
        if callable(market_clock):
            clock = await asyncio.to_thread(market_clock)
            local_now = now.tz_convert(NY_TZ)
            after_open_grace = local_now.hour * 60 + local_now.minute >= 9 * 60 + 50
            latest_complete = normalized["timestamp"].max() + pd.Timedelta(minutes=5)
            if (clock.get("is_open") and after_open_grace
                    and now - latest_complete > pd.Timedelta(minutes=15)):
                self.store.set_engine(False, "IEX 行情超过 15 分钟未更新；策略已暂停", "stale_market_data")
                self._stop.set()
                return

        account = await asyncio.to_thread(self.client.account_snapshot)
        equity = float(account["equity"])
        session_date = datetime.now(timezone.utc).astimezone(ZoneInfo(NY_TZ)).date().isoformat()
        baseline_equity = float(account.get("last_equity") or equity)
        baseline = self.store.daily_baseline(session_date, baseline_equity)
        if equity <= baseline * (1 - self.settings.daily_loss_limit_fraction):
            self.store.set_engine(False, "达到当日 2% 回撤熔断；等待人工检查", "daily_loss_limit")
            self._stop.set()
            return

        positions = await asyncio.to_thread(self.client.positions)
        position_map = {str(p["symbol"]).upper(): p for p in positions}
        total_exposure = sum(max(0.0, float(p.get("market_value", 0))) for p in positions)
        reserved_cash = 0.0

        for symbol, group in normalized.groupby("symbol", sort=True):
            group = group.sort_values("timestamp", kind="stable").tail(max(self.settings.slow_window + 3, 100)).copy()
            if group.empty:
                continue
            last = group.iloc[-1]
            bar_ts = pd.Timestamp(last["timestamp"])
            bar_key = bar_ts.isoformat()
            previous = self.store.watermark(str(symbol))
            if previous and bar_ts <= pd.Timestamp(previous):
                continue
            self.store.set_watermark(str(symbol), bar_key)

            # If bars were missed within the same session, advance the watermark but
            # discard that stale signal. Overnight/session-open gaps are expected.
            if previous:
                prev_ts = pd.Timestamp(previous)
                same_session = prev_ts.tz_convert(NY_TZ).date() == bar_ts.tz_convert(NY_TZ).date()
                if same_session and bar_ts - prev_ts > pd.Timedelta(minutes=5):
                    self.store.claim_bar(str(symbol), bar_key, "SKIP", None)
                    self.store.finish_bar(str(symbol), bar_key, "skipped_gap", "停机/行情缺口，不追补旧信号")
                    continue

            signals = add_sma_signals(group, self.settings.fast_window, self.settings.slow_window)
            signal = str(signals.iloc[-1]["signal"])
            client_order_id = f"qd-{str(symbol).lower()}-{bar_ts.strftime('%Y%m%d%H%M')}-{signal.lower()}"[:48]
            if not self.store.claim_bar(str(symbol), bar_key, signal, client_order_id if signal in {"BUY", "SELL"} else None):
                continue
            if signal not in {"BUY", "SELL"}:
                self.store.finish_bar(str(symbol), bar_key, "no_signal", "无交叉信号")
                continue

            current = position_map.get(str(symbol).upper())
            if signal == "BUY":
                if current:
                    self.store.finish_bar(str(symbol), bar_key, "ignored_existing", "已有多头持仓")
                    continue
                price = float(last["close"])
                gross_cap = max(0.0, equity * self.settings.max_gross_fraction - total_exposure - reserved_cash)
                target = min(equity * self.settings.per_symbol_fraction, gross_cap,
                             max(0.0, float(account["cash"]) - reserved_cash))
                estimated_fill = price * (1 + self.settings.slippage_bps / 10_000)
                qty = math.floor(max(0.0, target - self.settings.commission_per_order) / estimated_fill)
                if qty < 1:
                    self.store.finish_bar(str(symbol), bar_key, "risk_blocked", "仓位/现金上限不足 1 股")
                    continue
                reserved_cash += qty * estimated_fill + self.settings.commission_per_order
                side = "buy"
            else:
                if not current:
                    self.store.finish_bar(str(symbol), bar_key, "ignored_flat", "没有可退出的多头持仓")
                    continue
                qty = math.floor(float(current.get("qty", 0)))
                if qty < 1:
                    self.store.finish_bar(str(symbol), bar_key, "risk_blocked", "持仓数量不足 1 股")
                    continue
                side = "sell"

            try:
                order = await asyncio.to_thread(
                    self.client.submit_market_order, str(symbol), qty, side, client_order_id
                )
                self.store.finish_bar(
                    str(symbol), bar_key, "submitted",
                    f"order_id={order['id']};qty={qty};status={order['status']}"
                )
                logger.info("Paper order submitted: symbol=%s side=%s qty=%s client_order_id=%s",
                            symbol, side, qty, client_order_id)
                if side == "sell" and current:
                    total_exposure = max(0.0, total_exposure - float(current.get("market_value", 0)))
                elif side == "buy":
                    total_exposure += qty * float(last["close"])
            except Exception as exc:
                self.store.finish_bar(str(symbol), bar_key, "submit_error", "Paper 订单提交失败；该 bar 不重试")
                raise EnginePreflightError("Paper 订单提交失败，策略已暂停以防重复") from exc

    async def shutdown(self) -> None:
        """Stop the in-process task without clearing the persisted resume intent."""
        self._stop.set()
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def status(self) -> dict[str, Any]:
        state = self.store.engine_state()
        return {"enabled": bool(state["enabled"]), "note": state["note"],
                "paused_reason": state["paused_reason"], "auto_resume": self.settings.auto_resume}
