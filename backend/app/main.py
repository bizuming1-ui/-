from __future__ import annotations

import logging
import math
import secrets
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

import random as _random
import time as _time

from backend.app.alpaca_paper import AlpacaPaperClient
from backend.app.paper_engine import EnginePreflightError, PaperEngine
from backend.app.sz_market_client import SZPaperMarketClient
from backend.app.sz_portfolio import SZPortfolioStore
from backend.app.sz159502_engine import SZ159502Engine, SZEnginePreflightError
from backend.app.tonghuashun_market import demo_5min_bars_note, demo_snapshot, tonghuashun_status

# ── 实时报价合成脉冲（离线 Demo 占位，明确非实时）─────────────────────────
# 性能说明：热路径由 Python 向量化（numpy/pandas）承载，后续热点可渐进迁 Rust(PyO3)；
# 行情网关 Go 并发拉取已预留（go_gateway/），此处保持轻量可验证。
_SZ_TICK_STATE: dict[str, float | str] = {
    "price": 1.045,
    "prev_close": 1.042,
    "open": 1.041,
    "high": 1.055,
    "low": 1.036,
    "volume": 1_250_000.0,
    "turnover": 1_305_000.0,
}
from backend.app.security import clear_login_failures, login_rate_limited, record_login_failure, require_authenticated, verify_password
from backend.app.settings import Settings, get_settings
from backend.app.store import StateStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("quantdesk")
ROOT = Path(__file__).resolve().parents[2]


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class StartRequest(BaseModel):
    acknowledge_risk: bool = False


class SettingsRequest(BaseModel):
    fast_window: int = Field(ge=1, le=200)
    slow_window: int = Field(ge=2, le=500)
    symbols: list[str] = Field(min_length=1, max_length=30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    store = StateStore(settings.database_path)
    app.state.store = store
    app.state.alpaca = None
    app.state.engine = None
    # SZ 159502 纸面模拟器：离线优先，不默认轮询；Key 仅服务端环境变量
    app.state.sz_portfolio = SZPortfolioStore(settings.database_path, settings)
    # 预构建 Demo 5min bars 用于离线演示（合成、非真实）
    try:
        demo_bars = SZPaperMarketClient.build_demo_bars(days=3, symbol=settings.sz159502_symbol)
    except Exception:
        demo_bars = None
    app.state.sz_client = SZPaperMarketClient(settings, app.state.sz_portfolio, ths_client=None, demo_bars=demo_bars)
    app.state.sz_engine = SZ159502Engine(settings, app.state.sz_client, store)
    if settings.app_mode == "paper":
        client = AlpacaPaperClient(settings)
        engine = PaperEngine(settings, client, store)
        app.state.alpaca = client
        app.state.engine = engine
        resume = await engine.resume_after_restart()
        logger.info("Paper engine startup state: enabled=%s", resume["enabled"])
    logger.info("Quant Desk API started in %s mode", settings.app_mode)
    yield
    if app.state.engine:
        await app.state.engine.shutdown()
    if getattr(app.state, "sz_engine", None):
        await app.state.sz_engine.shutdown()
    logger.info("Quant Desk API stopped")


app = FastAPI(title="Quant Desk API", version="0.1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
_settings_at_import = get_settings()
app.add_middleware(
    SessionMiddleware,
    secret_key=_settings_at_import.session_secret,
    session_cookie="quant_session",
    max_age=60 * 60 * 12,
    same_site="strict",
    https_only=_settings_at_import.session_cookie_secure,
)
# Production accepts the configured Cloudflare hostname plus internal health checks.
allowed_hosts = ["*"]
if _settings_at_import.environment == "production":
    public_host = urlsplit(_settings_at_import.app_public_url).hostname
    allowed_hosts = [host for host in (public_host, "localhost", "127.0.0.1", "quant-api", "quant-web") if host]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data: blob:; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
    )
    if get_settings().environment == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _auth(request: Request, settings: Settings = Depends(_settings)) -> None:
    require_authenticated(request, settings)


def _csrf(request: Request) -> None:
    stored = request.session.get("csrf_token")
    supplied = request.headers.get("x-csrf-token")
    if not stored or not supplied or not secrets.compare_digest(str(stored), supplied):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请求校验失败，请刷新页面后重试")


@app.get("/api/health")
def health(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    return {
        "ok": True,
        "mode": settings.app_mode,
        "market_data_feed": "Alpaca IEX" if settings.app_mode == "paper" else "Demo fixture",
        "paper_only": True,
        "sz159502": {
            "symbol": settings.sz159502_symbol,
            "currency": settings.sz159502_currency,
            "poll_enabled": settings.sz159502_poll_enabled,
            "has_authorized_5min_history": settings.sz159502_has_authorized_5min_history,
            "demo_labeled": True,
        },
    }


@app.get("/api/public-config")
def public_config(request: Request) -> dict[str, str]:
    return {"public_url": _settings(request).app_public_url.rstrip("/")}


@app.get("/api/auth/session")
def auth_session(request: Request, settings: Settings = Depends(_settings)) -> dict[str, Any]:
    local_demo_bypass = settings.environment == "development" and settings.app_mode == "demo" and not settings.app_password
    authenticated = bool(request.session.get("authenticated")) or local_demo_bypass
    csrf = request.session.get("csrf_token")
    if authenticated and not csrf:
        csrf = secrets.token_urlsafe(24)
        request.session["csrf_token"] = csrf
    return {"authenticated": authenticated, "login_required": not local_demo_bypass, "csrf_token": csrf}


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request, settings: Settings = Depends(_settings)) -> dict[str, Any]:
    forwarded = request.headers.get("cf-connecting-ip", "").split(",", 1)[0].strip()
    address = forwarded or (request.client.host if request.client else "unknown")
    if login_rate_limited(address):
        raise HTTPException(status_code=429, detail="登录尝试过多，请 15 分钟后重试", headers={"Retry-After": "900"})
    if not verify_password(payload.password, settings):
        record_login_failure(address)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="口令不正确")
    clear_login_failures(address)
    request.session.clear()
    request.session["authenticated"] = True
    request.session["csrf_token"] = secrets.token_urlsafe(24)
    return {"ok": True, "csrf_token": request.session["csrf_token"]}


@app.post("/api/auth/logout")
def logout(request: Request, _: None = Depends(_auth)) -> dict[str, bool]:
    _csrf(request)
    request.session.clear()
    return {"ok": True}


@app.get("/api/dashboard")
async def dashboard(request: Request, _: None = Depends(_auth)) -> dict[str, Any]:
    settings = _settings(request)
    if settings.app_mode == "demo":
        return _demo_dashboard(settings)
    client: AlpacaPaperClient = request.app.state.alpaca
    engine: PaperEngine = request.app.state.engine
    try:
        account = await asyncio.to_thread(client.account_snapshot)
        positions = await asyncio.to_thread(client.positions)
    except Exception as exc:
        logger.exception("Alpaca Paper read failed")
        raise HTTPException(status_code=503, detail="暂时无法读取 Alpaca Paper 账户，请检查服务器端凭证与网络") from exc
    state = engine.status()
    watermarks = [request.app.state.store.watermark(symbol) for symbol in settings.strategy_symbols]
    latest_bar_at = max((value for value in watermarks if value), default=None)
    market_status = (
        "IEX 行情过期 · 策略暂停" if state.get("paused_reason") == "stale_market_data"
        else "策略运行 · 展示最近已处理完整 bar" if state.get("enabled")
        else "策略暂停 · 展示最近已处理完整 bar" if latest_bar_at
        else "尚无已处理 bar · 策略未运行"
    )
    events = request.app.state.store.recent_events(20)
    orders = []
    for event in events:
        if event["signal"] not in {"BUY", "SELL"} or event["state"] != "submitted":
            continue
        details = {}
        for item in str(event.get("message") or "").split(";"):
            if "=" in item:
                key, value = item.split("=", 1)
                details[key.strip()] = value.strip()
        bar_time = datetime.fromisoformat(str(event["bar_ts"]).replace("Z", "+00:00"))
        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=timezone.utc)
        orders.append({
            "time": bar_time.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M"),
            "symbol": event["symbol"], "side": event["signal"],
            "qty": int(details.get("qty", "0")),
            "status": details.get("status", "SUBMITTED"),
            "order_id": details.get("order_id"), "demo": False,
        })
    return {
        "mode": "paper", "feed": "IEX · 单一交易所覆盖",
        "updated_at": datetime.now(timezone.utc).isoformat(), "market_bar_at": latest_bar_at,
        "market_status": market_status, "account": account,
        "positions": positions, "watchlist": settings.strategy_symbols,
        "strategy": {"fast_window": settings.fast_window, "slow_window": settings.slow_window, "bar_minutes": 5},
        "engine": state, "equity_curve": [], "orders": orders, "metrics": None,
    }


@app.get("/api/settings", dependencies=[Depends(_auth)])
def read_settings(request: Request) -> dict[str, Any]:
    s = _settings(request)
    return {"fast_window": s.fast_window, "slow_window": s.slow_window, "symbols": s.strategy_symbols,
            "per_symbol_fraction": s.per_symbol_fraction, "max_gross_fraction": s.max_gross_fraction,
            "daily_loss_limit_fraction": s.daily_loss_limit_fraction, "slippage_bps": s.slippage_bps,
            "commission_per_order": s.commission_per_order}


@app.put("/api/settings", dependencies=[Depends(_auth)])
def update_settings(payload: SettingsRequest, request: Request) -> dict[str, Any]:
    _csrf(request)
    if payload.fast_window >= payload.slow_window:
        raise HTTPException(status_code=422, detail="快线窗口必须小于慢线窗口")
    normalized = list(dict.fromkeys(symbol.strip().upper() for symbol in payload.symbols if symbol.strip()))
    if not normalized or len(normalized) > 30:
        raise HTTPException(status_code=422, detail="观察清单需包含 1–30 个有效代码")
    current: Settings = request.app.state.settings
    if request.app.state.engine and request.app.state.engine.enabled:
        raise HTTPException(status_code=409, detail="策略运行中不能修改窗口或观察清单")
    current.fast_window, current.slow_window, current.strategy_symbols = payload.fast_window, payload.slow_window, normalized
    return {"ok": True, "fast_window": current.fast_window, "slow_window": current.slow_window,
            "symbols": current.strategy_symbols}


@app.get("/api/engine/status", dependencies=[Depends(_auth)])
def engine_status(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    engine = request.app.state.engine
    state = engine.status() if engine else {"enabled": False, "note": "演示模式；未连接券商", "paused_reason": None,
                                             "auto_resume": settings.auto_resume}
    return {**state, "mode": settings.app_mode}


@app.post("/api/engine/start", dependencies=[Depends(_auth)])
async def start_engine(request: Request, payload: StartRequest = StartRequest()) -> dict[str, Any]:
    _csrf(request)
    settings = _settings(request)
    engine: PaperEngine | None = request.app.state.engine
    if settings.app_mode != "paper" or engine is None:
        raise HTTPException(status_code=409, detail="演示模式不会向券商提交订单")
    previous = engine.status()
    if previous.get("paused_reason") == "daily_loss_limit" and not payload.acknowledge_risk:
        raise HTTPException(status_code=409, detail="当日回撤熔断已触发；请先阅读风险提示并明确确认")
    try:
        return await engine.start(acknowledge_risk=payload.acknowledge_risk)
    except EnginePreflightError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/engine/stop", dependencies=[Depends(_auth)])
async def stop_engine(request: Request) -> dict[str, Any]:
    _csrf(request)
    engine: PaperEngine | None = request.app.state.engine
    if engine is None:
        return {"enabled": False, "note": "演示模式；没有订单引擎"}
    return await engine.stop()


@app.get("/api/kaggle-code", dependencies=[Depends(_auth)])
def kaggle_code() -> FileResponse:
    path = ROOT / "kaggle" / "quant_research.py"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Kaggle 代码正在生成")
    return FileResponse(path, media_type="text/x-python; charset=utf-8", filename="quant_research.py")


@app.get("/api/kaggle-notebook", dependencies=[Depends(_auth)])
def kaggle_notebook() -> FileResponse:
    path = ROOT / "kaggle" / "quant_research.ipynb"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Kaggle Notebook 正在生成")
    return FileResponse(path, media_type="application/x-ipynb+json", filename="quant_research.ipynb")


@app.get("/api/sz159502/status", dependencies=[Depends(_auth)])
def sz_status(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    return tonghuashun_status(settings)


@app.get("/api/sz159502/snapshot", dependencies=[Depends(_auth)])
def sz_snapshot(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    # 离线 Demo 快照：清晰标注非实时；真实接口仅在 tonghuashun_enabled 且有 Key 时按需调用（此处不自动触发）
    return demo_snapshot(settings)


@app.get("/api/sz159502/bars-note", dependencies=[Depends(_auth)])
def sz_bars_note(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    return demo_5min_bars_note(settings)


@app.get("/api/sz159502/demo-dashboard", dependencies=[Depends(_auth)])
async def sz_demo_dashboard(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    # 159502 离线纸面仪表板：¥100,000 初始、10% 单笔、2% 日熔断（止新买+尝试卖出）、占位成本、允许隔夜
    portfolio = request.app.state.sz_portfolio
    # 用合成 demo bars 计算展示用权益曲线（非真实回测）
    from shared.quant_core import demo_sz159502_equity_curve
    equity_curve = demo_sz159502_equity_curve()
    account = portfolio.account_snapshot()
    positions = portfolio.positions()
    # 若无持仓，补充一个演示持仓示例以展示界面（明确 demo）
    if not positions:
        positions = [
            {"symbol": settings.sz159502_symbol, "qty": 800, "side": "long", "avg_entry_price": 1.021, "current_price": 1.045, "market_value": 836.0, "unrealized_pl": 19.2, "unrealized_plpc": 0.0235},
        ]
        # 调整权益以自洽
        account = {**account, "equity": 101820.0, "portfolio_value": 836.0, "cash": 100984.0}
    return {
        "mode": "demo",
        "offline": True,
        "symbol": settings.sz159502_symbol,
        "name": settings.sz159502_name,
        "market": settings.sz159502_market,
        "timezone": settings.sz159502_timezone,
        "currency": settings.sz159502_currency,
        "feed": "离线演示 · 非实时行情 · 未获授权 5 分钟真实数据不作回测",
        "disclaimer": "纸面模拟 · 合成演示 · 不构成投资建议",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "account": account,
        "positions": positions,
        "strategy": {"fast_window": settings.fast_window, "slow_window": settings.slow_window, "bar_minutes": 5, "side": "long-only", "execution": "下一根可交易 K 开盘成交", "overnight": "允许隔夜"},
        "risk": {
            "initial_equity": settings.sz159502_initial_equity,
            "per_trade_fraction": settings.sz159502_per_trade_fraction,
            "daily_loss_limit_fraction": settings.sz159502_daily_loss_limit_fraction,
            "behavior": "日回撤 2% 后停止新买入并尝试卖出可卖持仓",
        },
        "cost": {"slippage_bps": settings.sz159502_slippage_bps, "commission_per_order": settings.sz159502_commission_per_order, "note": settings.sz159502_cost_note, "is_placeholder": True},
        "engine": request.app.state.sz_engine.status(),
        "equity_curve": equity_curve,
        "orders": portfolio.recent_orders(10),
        "unverified": ["同花顺 Playground code=0 待确认费用/额度/授权", "历史基金接口日线；159502 五分钟历史未授权不回测", "实时轮询默认关闭；Key 仅服务端环境变量"],
    }


@app.post("/api/sz159502/engine/start", dependencies=[Depends(_auth)])
async def sz_engine_start(request: Request, payload: StartRequest = StartRequest()) -> dict[str, Any]:
    _csrf(request)
    settings = _settings(request)
    engine: SZ159502Engine = request.app.state.sz_engine
    if not settings.sz159502_poll_enabled:
        raise HTTPException(status_code=409, detail="159502 实时轮询默认关闭；请先确认授权与风控后再显式开启（当前为离线 Demo）")
    previous = engine.status()
    if previous.get("paused_reason") == "daily_loss_limit" and not payload.acknowledge_risk:
        raise HTTPException(status_code=409, detail="当日回撤熔断已触发；请先阅读风险提示并明确确认")
    try:
        return await engine.start(acknowledge_risk=payload.acknowledge_risk)
    except SZEnginePreflightError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/sz159502/engine/stop", dependencies=[Depends(_auth)])
async def sz_engine_stop(request: Request) -> dict[str, Any]:
    _csrf(request)
    engine: SZ159502Engine | None = getattr(request.app.state, "sz_engine", None)
    if engine is None:
        return {"enabled": False, "note": "演示模式；没有订单引擎"}
    return await engine.stop()


def _sz_next_tick_price() -> float:
    # 轻量确定性随机游走：均值回归至 1.045，步长 ±0.004，避免越界
    base = 1.045
    cur = float(_SZ_TICK_STATE["price"])
    drift = (base - cur) * 0.03  # 均值回归
    step = (_random.random() - 0.5) * 0.006
    nxt = max(0.78, min(1.32, cur + drift + step))
    _SZ_TICK_STATE["price"] = round(nxt, 4)
    # 更新日内高低
    _SZ_TICK_STATE["high"] = round(max(float(_SZ_TICK_STATE["high"]), nxt), 4)
    _SZ_TICK_STATE["low"] = round(min(float(_SZ_TICK_STATE["low"]), nxt), 4)
    _SZ_TICK_STATE["volume"] = round(float(_SZ_TICK_STATE["volume"]) + _random.randint(8000, 24000), 0)
    return nxt

def _build_sz_realtime(request: Request) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    nxt_price = _sz_next_tick_price()
    prev_close = float(_SZ_TICK_STATE["prev_close"])
    change = nxt_price - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0.0
    # 从 demo bars 取最近 50 根计算 SMA（纯 Python 向量化，后续可切 Rust）
    try:
        demo_bars = getattr(request.app.state.sz_client, "demo_bars", None)
        if demo_bars is not None and hasattr(demo_bars, "__len__") and len(demo_bars) > 0:
            import pandas as pd
            closes = demo_bars["close"].tail(50).tolist() if hasattr(demo_bars, "iloc") else []
            closes.append(nxt_price)
            # 简化 SMA：取最后 20/50
            def _sma(vals, w):
                if len(vals) < w:
                    return None
                return round(sum(vals[-w:]) / w, 4)
            sma_fast = _sma(closes, settings.fast_window)
            sma_slow = _sma(closes, settings.slow_window)
            if sma_fast is not None and sma_slow is not None:
                signal = "BUY" if sma_fast > sma_slow else "SELL" if sma_fast < sma_slow else "HOLD"
            else:
                signal = "HOLD"
        else:
            sma_fast = round(nxt_price * 0.998, 4)
            sma_slow = round(nxt_price * 1.002, 4)
            signal = "HOLD"
    except Exception:
        sma_fast, sma_slow, signal = None, None, "HOLD"
    portfolio = getattr(request.app.state, "sz_portfolio", None)
    account = portfolio.account_snapshot() if portfolio else {"equity": 101820, "cash": 100984, "portfolio_value": 836, "currency": "CNY"}
    positions = portfolio.positions() if portfolio else []
    # 若空仓，展示合成示例以保持大屏饱满（明确 demo）
    if not positions:
        positions = [{"symbol": settings.sz159502_symbol, "qty": 800, "side": "long", "avg_entry_price": 1.021, "current_price": nxt_price, "market_value": round(800 * nxt_price, 2), "unrealized_pl": round(800 * (nxt_price - 1.021), 2), "unrealized_plpc": round((nxt_price / 1.021 - 1), 4)}]
    engine = getattr(request.app.state, "sz_engine", None)
    eng_state = engine.status() if engine else {"enabled": False, "note": "离线 Demo"}
    account_equity = float(account.get("equity", 100000))
    initial = float(settings.sz159502_initial_equity)
    daily_ret = (account_equity / initial - 1) * 100 if initial else 0.0
    dd = max(0.0, -daily_ret) if daily_ret < 0 else 0.0
    drawdown_pct = round(dd, 2)
    can_trade = dd < float(settings.sz159502_daily_loss_limit_fraction) * 100
    # 下一根开盘估算（当前价 + 滑点占位）
    slip = float(settings.sz159502_slippage_bps) / 10_000
    next_open_est = round(nxt_price * (1 + slip) if signal == "BUY" else nxt_price * (1 - slip) if signal == "SELL" else nxt_price, 4)
    return {
        "symbol": settings.sz159502_symbol,
        "name": settings.sz159502_name,
        "currency": settings.sz159502_currency,
        "market": settings.sz159502_market,
        "timezone": settings.sz159502_timezone,
        "is_demo": True,
        "is_realtime_placeholder": True,
        "note": "合成脉冲 · 离线演示 · 非实时行情 · 成本占位",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "price": nxt_price,
        "prev_close": prev_close,
        "open": float(_SZ_TICK_STATE["open"]),
        "high": float(_SZ_TICK_STATE["high"]),
        "low": float(_SZ_TICK_STATE["low"]),
        "volume": int(float(_SZ_TICK_STATE["volume"])),
        "turnover": float(_SZ_TICK_STATE["turnover"]),
        "change": round(change, 4),
        "change_pct": round(change_pct, 2),
        "sma_fast": sma_fast,
        "sma_slow": sma_slow,
        "signal": signal,
        "next_open_estimate": next_open_est,
        "next_bar_execution": "下一根可交易 K 开盘成交（深圳 09:30-11:30/13:00-15:00）",
        "account": account,
        "positions": positions,
        "engine": eng_state,
        "risk": {"initial_equity": initial, "daily_drawdown_pct": drawdown_pct, "daily_loss_limit_pct": float(settings.sz159502_daily_loss_limit_fraction) * 100, "can_trade": can_trade, "behavior": "2% 止新买并尝试卖出"},
        "cost": {"slippage_bps": settings.sz159502_slippage_bps, "commission_per_order": settings.sz159502_commission_per_order, "note": settings.sz159502_cost_note, "is_placeholder": True},
        "orders": portfolio.recent_orders(8) if portfolio else [],
        "unverified": ["同花顺 Playground code=0 待确认费用/额度", "历史基金接口日线；无授权 5min 不回测", "轮询默认关闭；Key 仅服务端环境变量"],
    }

@app.get("/api/sz159502/realtime", dependencies=[Depends(_auth)])
def sz_realtime(request: Request) -> dict[str, Any]:
    return _build_sz_realtime(request)

@app.get("/api/sz159502/ticker", dependencies=[Depends(_auth)])
def sz_ticker(request: Request) -> dict[str, Any]:
    snap = _build_sz_realtime(request)
    return {k: snap[k] for k in ("symbol","price","prev_close","open","high","low","volume","change","change_pct","timestamp","sma_fast","sma_slow","signal","is_demo")}

@app.get("/api/sz159502/engine/status", dependencies=[Depends(_auth)])
def sz_engine_status(request: Request) -> dict[str, Any]:
    engine = getattr(request.app.state, "sz_engine", None)
    if not engine:
        return {"enabled": False, "note": "演示模式；未连接行情"}
    return engine.status()


def _demo_dashboard(settings: Settings) -> dict[str, Any]:
    # Deterministic demo fixtures; labels explicitly state that these are not quotes.
    points = []
    for i in range(78):
        progress = i / 77
        wave = 180 * math.sin(progress * math.pi * 6) * math.sin(progress * math.pi)
        minute = 9 * 60 + 30 + i * 5
        equity_value = 100_000 + 2_480 * progress + wave
        benchmark_value = 100_000 + 1_600 * progress + 80 * math.sin(progress * math.pi * 3)
        points.append({"time": f"{minute // 60:02d}:{minute % 60:02d}",
                       "equity": round(equity_value, 2), "benchmark": round(benchmark_value, 2)})
    return {
        "mode": "demo", "feed": "演示数据 · 非实时行情", "updated_at": datetime.now(timezone.utc).isoformat(),
        "market_bar_at": None, "market_status": "演示样例 · 非实时行情",
        "account": {"status": "DEMO", "cash": 86_799.40, "equity": 102_480.00, "last_equity": 100_000.00,
                    "buying_power": 86_799.40, "portfolio_value": 102_480.00, "currency": "USD",
                    "pattern_day_trader": False, "trading_blocked": False},
        "positions": [
            {"symbol": "AAPL", "qty": 20, "side": "long", "avg_entry_price": 183.2799,
             "current_price": 219.42, "market_value": 4388.40, "unrealized_pl": 722.80, "unrealized_plpc": 0.1972},
            {"symbol": "MSFT", "qty": 12, "side": "long", "avg_entry_price": 422.7758,
             "current_price": 501.55, "market_value": 6018.60, "unrealized_pl": 945.29, "unrealized_plpc": 0.1863},
            {"symbol": "SPY", "qty": 8, "side": "long", "avg_entry_price": 557.7116,
             "current_price": 659.20, "market_value": 5273.60, "unrealized_pl": 811.91, "unrealized_plpc": 0.1820},
        ],
        "watchlist": settings.strategy_symbols,
        "strategy": {"fast_window": settings.fast_window, "slow_window": settings.slow_window, "bar_minutes": 5},
        "engine": {"enabled": False, "note": "演示模式；未连接券商"}, "equity_curve": points,
        "orders": [
            {"time": "10:35", "symbol": "AAPL", "side": "BUY", "qty": 20, "status": "FILLED", "demo": True},
            {"time": "09:55", "symbol": "MSFT", "side": "BUY", "qty": 12, "status": "FILLED", "demo": True},
            {"time": "09:40", "symbol": "SPY", "side": "BUY", "qty": 8, "status": "FILLED", "demo": True},
        ],
        "metrics": {"total_return": 0.0248, "max_drawdown": -0.0004415, "sharpe": None, "win_rate": None},
    }
