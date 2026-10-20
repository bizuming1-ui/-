from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from shared.quant_core import SZ_TZ, add_sma_signals, normalize_ohlcv

logger = logging.getLogger("quantdesk.sz159502")

class SZEnginePreflightError(RuntimeError):
    pass

class SZ159502Engine:
    """159502 单标的纸面引擎：5 分钟 SMA20/50、只做多、下一根可交易 K 开盘成交、允许隔夜。

    风控：单笔≤权益10%、日回撤2%后停止新买入并尝试卖出可卖持仓。
    成本均为可配置占位值，非真实费率；离线/Demo 醒目标注；不默认轮询。
    """

    def __init__(self, settings, client, store) -> None:
        self.settings = settings
        self.client = client
        self.store = store
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.store.engine_state()["enabled"])

    async def start(self, *, after_restart: bool = False, acknowledge_risk: bool = False) -> dict[str, Any]:
        async with self._lock:
            if self.enabled and self._task and not self._task.done():
                return {"enabled": True, "note": self.store.engine_state()["note"]}
            try:
                prior = self.store.engine_state()
                if not after_restart and prior.get("paused_reason") == "daily_loss_limit":
                    if not acknowledge_risk:
                        raise SZEnginePreflightError("当日回撤熔断已触发；需明确确认风险后才能重新启用")
                    # 重置当日基线
                    account = await asyncio.to_thread(self.client.account_snapshot)
                    day = datetime.now(timezone.utc).astimezone(ZoneInfo(SZ_TZ)).date().isoformat()
                    self.store.reset_daily_baseline(day, float(account["equity"]))
                await self._reconcile_account()
                await self._prime_watermarks()
                self.store.set_engine(True, "已启用 159502 纸面策略；等待下一根完整 5 分钟 bar", None)
                self._stop = asyncio.Event()
                self._task = asyncio.create_task(self._run(), name="sz159502-engine")
                return {"enabled": True, "note": self.store.engine_state()["note"]}
            except Exception as exc:
                self.store.set_engine(False, "159502 预检失败；策略保持暂停", "preflight")
                if isinstance(exc, SZEnginePreflightError):
                    raise
                logger.exception("SZ159502 preflight failed")
                raise SZEnginePreflightError("159502 预检失败；检查账户、行情订阅与网络") from exc

    async def resume_after_restart(self) -> dict[str, Any]:
        state = self.store.engine_state()
        if not state["enabled"]:
            return {"enabled": False, "note": state["note"]}
        if not self.settings.auto_resume:
            self.store.set_engine(False, "服务重启后按配置保持暂停", "auto_resume_disabled")
            return {"enabled": False, "note": self.store.engine_state()["note"]}
        if state.get("paused_reason"):
            self.store.set_engine(False, "先前触发安全暂停；需人工检查后恢复", state["paused_reason"])
            return {"enabled": False, "note": self.store.engine_state()["note"]}
        try:
            return await self.start(after_restart=True)
        except Exception:
            logger.exception("SZ159502 auto resume failed")
            return {"enabled": False, "note": self.store.engine_state()["note"]}

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
            raise SZEnginePreflightError("纸面账户当前被禁止交易")
        if float(account.get("equity", 0)) <= 0 or float(account.get("cash", 0)) < 0:
            raise SZEnginePreflightError("纸面账户权益/现金异常")
        orders = await asyncio.to_thread(self.client.open_orders)
        if orders:
            raise SZEnginePreflightError("存在未完成订单；先在账户中检查并处理")
        positions = await asyncio.to_thread(self.client.positions)
        for p in positions:
            symbol = str(p["symbol"]).upper()
            if symbol != "159502.SZ" and symbol != "159502":
                raise SZEnginePreflightError(f"发现非 159502 持仓 {symbol}；单标的模拟器仅允许 159502.SZ")
            side = str(p.get("side", "long")).lower()
            qty = float(p.get("qty", 0))
            if "short" in side or qty < 0:
                raise SZEnginePreflightError("发现空头仓位；本策略只允许做多")
            if not math.isclose(qty, round(qty), abs_tol=1e-8):
                raise SZEnginePreflightError("发现非整股仓位；请先人工对账")

    async def _prime_watermarks(self) -> None:
        bars = await asyncio.to_thread(self.client.latest_bars, [self.settings.sz159502_symbol], 60 * 24 * 7)
        if bars.empty:
            raise SZEnginePreflightError("没有返回可用的 159502 历史 bar；策略不会启动")
        normalized = normalize_ohlcv(bars, regular_hours_only=True, market="sz")
        now = pd.Timestamp.now(tz="UTC")
        normalized = normalized.loc[normalized["timestamp"] + pd.Timedelta(minutes=5) <= now]
        if normalized.empty:
            raise SZEnginePreflightError("没有已完成的深圳常规时段 bar；策略不会启动")
        for symbol, group in normalized.groupby("symbol"):
            latest = group["timestamp"].max().isoformat()
            self.store.set_watermark(str(symbol), latest)

    async def _run(self) -> None:
        logger.info("SZ159502 engine loop started")
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("SZ159502 engine error; fail closed")
                self.store.set_engine(False, "行情/账户处理异常，已暂停新订单", "engine_error")
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.sz159502_poll_seconds)
            except asyncio.TimeoutError:
                pass
        logger.info("SZ159502 engine loop stopped")

    async def _poll_once(self) -> None:
        # 仅在显式启用轮询时才应被调度；此处做二次保护
        if not self.settings.sz159502_poll_enabled and not self.settings.tonghuashun_enabled:
            # 离线 Demo 下 _poll_once 不应被定时器调用；若被调用则直接返回并标记说明
            return
        bars = await asyncio.to_thread(self.client.latest_bars, [self.settings.sz159502_symbol], 60 * 24 * 7)
        if bars.empty:
            raise SZEnginePreflightError("行情返回空数据；策略已暂停")
        normalized = normalize_ohlcv(bars, regular_hours_only=True, market="sz")
        now = pd.Timestamp.now(tz="UTC")
        # 仅使用已完成 bar
        normalized = normalized.loc[normalized["timestamp"] + pd.Timedelta(minutes=5) <= now]
        if normalized.empty:
            return

        # 行情过期检测（深圳时段）
        market_clock = getattr(self.client, "market_clock", None)
        if callable(market_clock):
            clock = await asyncio.to_thread(market_clock)
            local_now = now.tz_convert(SZ_TZ)
            # 深圳开盘后 09:50 起视为交易时段内
            is_trading_minutes = (9*60+30 <= local_now.hour*60+local_now.minute <= 11*60+30) or (13*60 <= local_now.hour*60+local_now.minute <= 15*60)
            latest_complete = normalized["timestamp"].max() + pd.Timedelta(minutes=5)
            if clock.get("is_open") and is_trading_minutes and now - latest_complete > pd.Timedelta(minutes=15):
                self.store.set_engine(False, "行情超过 15 分钟未更新；策略已暂停", "stale_market_data")
                self._stop.set()
                return

        account = await asyncio.to_thread(self.client.account_snapshot)
        equity = float(account["equity"])
        cash = float(account["cash"])
        session_date = datetime.now(timezone.utc).astimezone(ZoneInfo(SZ_TZ)).date().isoformat()
        baseline_equity = float(account.get("last_equity") or equity)
        baseline = self.store.daily_baseline(session_date, baseline_equity)
        daily_limit = self.settings.sz159502_daily_loss_limit_fraction
        daily_drawdown_triggered = equity <= baseline * (1 - daily_limit)

        if daily_drawdown_triggered:
            # 记录风控状态：停止新买入，但允许卖出；不立即 stop，以便本轮尝试卖出
            # 注意：不在此处 set _stop，而是标记 paused_reason，下一轮仍会评估是否继续卖出
            self.store.set_engine(False, "达到当日 2% 回撤熔断；已停止新买入并尝试卖出可卖持仓", "daily_loss_limit")
            # 不 return，继续处理 SELL，避免持有风险敞口无法退出

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

            # 缺口检测：同会话内跳过旧信号，隔夜/跨午休为预期
            if previous:
                prev_ts = pd.Timestamp(previous)
                same_session = prev_ts.tz_convert(SZ_TZ).date() == bar_ts.tz_convert(SZ_TZ).date()
                # 深圳日历需特殊处理午休跨度，但简化为 >5 分钟即跳过同会话缺口
                if same_session and bar_ts - prev_ts > pd.Timedelta(minutes=5):
                    # 需区分午休：11:30 -> 13:00 间隙为正常，不算缺口
                    prev_local = prev_ts.tz_convert(SZ_TZ)
                    cur_local = bar_ts.tz_convert(SZ_TZ)
                    prev_min = prev_local.hour*60+prev_local.minute
                    cur_min = cur_local.hour*60+cur_local.minute
                    is_lunch_gap = (prev_min == 11*60+25 and cur_min == 13*60) or (prev_min >= 11*60+30 and cur_min <= 13*60)
                    if not is_lunch_gap:
                        self.store.claim_bar(str(symbol), bar_key, "SKIP", None)
                        self.store.finish_bar(str(symbol), bar_key, "skipped_gap", "停机/行情缺口，不追补旧信号")
                        continue

            signals = add_sma_signals(group, self.settings.fast_window, self.settings.slow_window)
            signal = str(signals.iloc[-1]["signal"])
            client_order_id = f"sz159502-{bar_ts.strftime('%Y%m%d%H%M')}-{signal.lower()}"[:48]
            if not self.store.claim_bar(str(symbol), bar_key, signal, client_order_id if signal in {"BUY", "SELL"} else None):
                continue
            if signal not in {"BUY", "SELL"}:
                # 若已触发日回撤且持有仓位，尝试主动卖出（非信号驱动的风控卖出）
                if daily_drawdown_triggered:
                    current = position_map.get(str(symbol).upper())
                    if current and float(current.get("qty", 0)) > 0:
                        # 风控卖出：即便无 SELL 信号，也尝试以当前 bar 收盘价估算卖出
                        qty = math.floor(float(current.get("qty", 0)))
                        if qty >= 1:
                            try:
                                order = await asyncio.to_thread(
                                    self.client.submit_market_order, str(symbol), qty, "sell", client_order_id + "-risk"
                                )
                                self.store.finish_bar(str(symbol), bar_key, "submitted", f"order_id={order['id']};qty={qty};status={order['status']};reason=daily_loss_limit")
                                logger.info("SZ159502 risk sell after drawdown symbol=%s qty=%s", symbol, qty)
                                total_exposure = max(0.0, total_exposure - float(current.get("market_value", 0)))
                            except Exception as exc:
                                self.store.finish_bar(str(symbol), bar_key, "submit_error", "风控卖出提交失败")
                                raise SZEnginePreflightError("风控卖出提交失败") from exc
                            continue
                self.store.finish_bar(str(symbol), bar_key, "no_signal", "无交叉信号")
                continue

            current = position_map.get(str(symbol).upper())
            # 日回撤后禁止新买入
            if signal == "BUY":
                if daily_drawdown_triggered:
                    self.store.finish_bar(str(symbol), bar_key, "risk_blocked", "日回撤 2% 熔断：已停止新买入")
                    continue
                if current:
                    self.store.finish_bar(str(symbol), bar_key, "ignored_existing", "已有多头持仓")
                    continue
                price = float(last["close"])
                # 单笔≤权益 10%，总敞口不另设（单标的场景下总敞口≈单标的敞口，但为一致性保留 50% 检查）
                equity_cap = equity * self.settings.sz159502_per_trade_fraction
                gross_cap = max(0.0, equity * self.settings.max_gross_fraction - total_exposure - reserved_cash) if hasattr(self.settings, "max_gross_fraction") else equity_cap
                target = min(equity_cap, gross_cap, max(0.0, cash - reserved_cash))
                estimated_fill = price * (1 + self.settings.sz159502_slippage_bps / 10_000)
                qty = math.floor(max(0.0, target - self.settings.sz159502_commission_per_order) / estimated_fill)
                if qty < 1:
                    self.store.finish_bar(str(symbol), bar_key, "risk_blocked", "仓位/现金上限不足 1 股（占位成本已计）")
                    continue
                reserved_cash += qty * estimated_fill + self.settings.sz159502_commission_per_order
                side = "buy"
            else:  # SELL 允许在风控后执行
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
                self.store.finish_bar(str(symbol), bar_key, "submitted", f"order_id={order['id']};qty={qty};status={order['status']}")
                logger.info("SZ159502 order submitted symbol=%s side=%s qty=%s", symbol, side, qty)
                if side == "sell" and current:
                    total_exposure = max(0.0, total_exposure - float(current.get("market_value", 0)))
                elif side == "buy":
                    total_exposure += qty * float(last["close"])
            except Exception as exc:
                self.store.finish_bar(str(symbol), bar_key, "submit_error", "订单提交失败；该 bar 不重试")
                raise SZEnginePreflightError("订单提交失败，策略已暂停") from exc

        # 若当日已触发熔断，本轮卖出尝试后保持暂停状态（不自动恢复），需人工确认
        if daily_drawdown_triggered:
            self._stop.set()

    async def shutdown(self) -> None:
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
        return {"enabled": bool(state["enabled"]), "note": state["note"], "paused_reason": state["paused_reason"], "auto_resume": self.settings.auto_resume, "market": "SZSE", "symbol": self.settings.sz159502_symbol}
