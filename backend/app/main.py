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

from backend.app.alpaca_paper import AlpacaPaperClient
from backend.app.paper_engine import EnginePreflightError, PaperEngine
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
