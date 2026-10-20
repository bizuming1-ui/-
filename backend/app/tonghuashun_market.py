from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from backend.app.settings import Settings

logger = logging.getLogger("quantdesk.ths")

# 同花顺 159502 适配器：离线优先、Demo 清晰标注、不默认轮询
# - Key 仅从服务端环境变量 TONGHUASHUN_API_KEY 读取，绝不经前端/浏览器/GitHub/Notebook
# - 历史基金接口为日线；高频分钟接口在未授权前不得假装可用
# - 没有已授权的 159502 五分钟历史数据时，不做真实数据回测（仅返回离线说明）

DEFAULT_THS_SNAPSHOT_PATH = "/api/fund/market/snapshot"

# 用于文档/状态说明的占位基线，不代表真实成交
DEMO_SNAPSHOT: dict[str, Any] = {
    "thscode": "159502.SZ",
    "name": "嘉实标普生物科技精选行业 ETF（QDII）",
    "market": "SZSE",
    "currency": "CNY",
    "mode": "demo",
    "is_offline_demo": True,
    "disclaimer": "离线 Demo 数据 · 非实时行情 · 非投资建议",
    "quote": None,
}

UNVERIFIED_ITEMS = [
    "同花顺返回 code=0 仅为 Playground 手动查询，费用、额度、授权范围尚未确认",
    "历史基金接口为日线；159502 的 5 分钟历史数据未获授权，不做真实回测",
    "高频分钟接口不应被假装为可用；实时轮询默认关闭",
    "Key 仅服务端环境变量 TONGHUASHUN_API_KEY 可用，前端/Notebook/GitHub 不得保存",
    "成本为可配置占位值，非真实费率",
]


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def tonghuashun_status(settings: Settings) -> dict[str, Any]:
    """返回对前端安全的、脱敏的同花顺接入状态（绝不返回 Key）。"""
    has_key = bool(settings.tonghuashun_api_key)
    return {
        "symbol": settings.sz159502_symbol,
        "name": settings.sz159502_name,
        "market": settings.sz159502_market,
        "currency": settings.sz159502_currency,
        "timezone": settings.sz159502_timezone,
        "initial_equity": settings.sz159502_initial_equity,
        "per_trade_fraction": settings.sz159502_per_trade_fraction,
        "daily_loss_limit_fraction": settings.sz159502_daily_loss_limit_fraction,
        "cost_note": settings.sz159502_cost_note,
        "cost": {
            "slippage_bps": settings.sz159502_slippage_bps,
            "commission_per_order": settings.sz159502_commission_per_order,
            "note": settings.sz159502_cost_note,
            "is_placeholder": True,
        },
        "strategy": {
            "bar_minutes": 5,
            "sma_fast": settings.fast_window,
            "sma_slow": settings.slow_window,
            "side": "long-only",
            "execution": "下一根可交易 K 线开盘成交（已完成 bar 触发）",
            "overnight": "允许隔夜持仓",
        },
        "tonghuashun": {
            "enabled": bool(settings.tonghuashun_enabled),
            "has_key_on_server": has_key,
            "key_source": "TONGHUASHUN_API_KEY (server env only)" if has_key else "not configured",
            "poll_enabled": bool(settings.sz159502_poll_enabled),
            "poll_seconds": settings.sz159502_poll_seconds,
            "note": "实时轮询默认关闭；启用需显式配置并确认授权范围",
        },
        "data_availability": {
            "has_authorized_5min_history": bool(settings.sz159502_has_authorized_5min_history),
            "minute_history_note": (
                "已授权 159502 五分钟历史数据" if settings.sz159502_has_authorized_5min_history
                else "未获授权的 159502 五分钟历史数据 · 不做真实回测 · 仅离线演示"
            ),
            "historical_fund_interface": "日线（daily）",
            "minute_bar_assumption": "高频分钟接口不应被假装为可用",
            "unverified_items": UNVERIFIED_ITEMS,
        },
        "disclaimer": "纸面模拟 · 离线/Demo 优先 · 不构成投资建议",
        "checked_at": _now_utc_iso(),
    }


def demo_snapshot(settings: Settings | None = None) -> dict[str, Any]:
    """离线 Demo 快照（合成示例，清晰标注非实时）。"""
    symbol = settings.sz159502_symbol if settings else DEMO_SNAPSHOT["thscode"]
    name = settings.sz159502_name if settings else DEMO_SNAPSHOT["name"]
    return {
        "thscode": symbol,
        "name": name,
        "code": 0,
        "mode": "demo",
        "is_offline_demo": True,
        "is_real_api": False,
        "disclaimer": "离线 Demo 快照 · 非实时行情 · 仅用于界面演示",
        "snapshot": {
            "last_price": 1.023,
            "prev_close": 1.015,
            "open": 1.018,
            "high": 1.027,
            "low": 1.016,
            "volume": 12345600,
            "amount": 12600000.0,
            "nav": 1.022,
            "iopv": 1.023,
            "premium_discount": "—",
        },
        "cost_note": settings.sz159502_cost_note if settings else DEMO_SNAPSHOT.get("disclaimer"),
        "as_of": _now_utc_iso(),
        "source": "offline-fixture",
    }


def demo_5min_bars_note(settings: Settings) -> dict[str, Any]:
    return {
        "symbol": settings.sz159502_symbol,
        "available": False,
        "reason": "没有已授权的 159502 五分钟历史数据时，不做真实数据回测",
        "interface": "历史基金接口为日线；分钟级数据未假装为可用",
        "has_authorized_5min_history": settings.sz159502_has_authorized_5min_history,
        "action": "保持离线/Demo 演示；如需接入，需先完成授权并显式开启",
    }


class TonghuashunClient:
    """服务端同花顺客户端：Key 仅从环境变量读取，按需调用，不默认轮询。"""

    def __init__(self, settings: Settings, base_url: str | None = None) -> None:
        self.settings = settings
        # base_url 仅在显式启用时使用；默认不发起真实调用
        self.base_url = (base_url or "").rstrip("/")
        if settings.tonghuashun_enabled and not settings.tonghuashun_api_key:
            raise ValueError("TONGHUASHUN_API_KEY 缺失：启用同花顺接口需在服务端配置环境变量")

    def _require_enabled(self) -> None:
        if not self.settings.tonghuashun_enabled:
            raise RuntimeError("同花顺接口未启用（tonghuashun_enabled=false）；保持离线 Demo")
        if not self.settings.tonghuashun_api_key:
            raise RuntimeError("服务端未配置 TONGHUASHUN_API_KEY；不会从前端/浏览器读取")

    def snapshot(self, thscode: str = "159502.SZ", timeout: float = 8.0) -> dict[str, Any]:
        """按需请求 /api/fund/market/snapshot?thscode=...（需显式启用且服务端有 Key）。"""
        self._require_enabled()
        if not self.base_url:
            raise RuntimeError("未配置同花顺 base_url；为避免误用，未发起真实请求")
        url = f"{self.base_url}{DEFAULT_THS_SNAPSHOT_PATH}"
        params = {"thscode": thscode}
        headers = {"Authorization": f"Bearer {self.settings.tonghuashun_api_key}"} if self.settings.tonghuashun_api_key else {}
        # 显式提醒：费用/额度/授权范围尚未确认，调用结果需人工核验
        logger.info("THS snapshot request thscode=%s base_url=%s (unverified quota/fees)", thscode, self.base_url)
        try:
            resp = httpx.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            # 不在此处做业务断言；code=0 仅为 Playground 手动验证结果
            return {
                "thscode": thscode,
                "code": data.get("code"),
                "raw": data,
                "is_real_api": True,
                "is_offline_demo": False,
                "disclaimer": "实时结果需核验费用/额度/授权范围；成本仍为占位值",
                "fetched_at": _now_utc_iso(),
            }
        except Exception as exc:
            logger.warning("THS snapshot failed thscode=%s: %s", thscode, exc)
            raise

    def latest_5min_bars(self, thscode: str = "159502.SZ") -> dict[str, Any]:
        """159502 五分钟历史：未授权时不提供、不假装可用。"""
        if not self.settings.sz159502_has_authorized_5min_history:
            return {
                "thscode": thscode,
                "available": False,
                "bars": [],
                "reason": "没有已授权的 159502 五分钟历史数据时，不做真实数据回测",
                "note": "历史基金接口是日线；高频分钟接口不应被假装为可用",
                "unverified_items": UNVERIFIED_ITEMS,
            }
        # 即便已授权，也要求显式启用，且不默认轮询
        self._require_enabled()
        # 实际分钟接口路径/权限待确认，此处仅占位，不伪造可用性
        return {
            "thscode": thscode,
            "available": False,
            "bars": [],
            "reason": "分钟级历史接口路径/额度待确认；当前保持占位，不返回伪造数据",
            "has_authorized_5min_history": True,
        }
