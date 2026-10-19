from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from backend.app.main import app


async def with_client(scenario: Callable[[httpx.AsyncClient], Awaitable[Any]]) -> Any:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await scenario(client)


def test_demo_api_is_explicitly_labeled_and_readable() -> None:
    async def scenario(client: httpx.AsyncClient) -> None:
        health = await client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["mode"] == "demo"
        assert health.json()["paper_only"] is True
        assert (await client.get("/docs")).status_code == 404
        dashboard = await client.get("/api/dashboard")
        assert dashboard.status_code == 200
        assert dashboard.json()["mode"] == "demo"
        assert "非实时行情" in dashboard.json()["feed"]
        assert dashboard.json()["engine"]["enabled"] is False
        body = dashboard.json()
        assert body["account"]["cash"] + sum(p["market_value"] for p in body["positions"]) == body["account"]["equity"]
        initial_capital = body["account"]["cash"] + sum(p["avg_entry_price"] * p["qty"] for p in body["positions"])
        assert round(initial_capital, 2) == 100_000
        assert round(sum(p["unrealized_pl"] for p in body["positions"]), 2) == 2_480
        assert body["equity_curve"][0]["equity"] == 100_000
        assert body["equity_curve"][-1]["equity"] == body["account"]["equity"]
        assert body["equity_curve"][-1]["benchmark"] == 101_600
    asyncio.run(with_client(scenario))


def test_demo_mode_refuses_order_engine_start() -> None:
    async def scenario(client: httpx.AsyncClient) -> None:
        session = (await client.get("/api/auth/session")).json()
        response = await client.post("/api/engine/start", json={}, headers={"X-CSRF-Token": session["csrf_token"]})
        assert response.status_code == 409
        assert "演示模式" in response.json()["detail"]
    asyncio.run(with_client(scenario))


def test_public_config_and_security_headers() -> None:
    async def scenario(client: httpx.AsyncClient) -> None:
        response = await client.get("/api/public-config")
        assert response.status_code == 200
        assert response.json()["public_url"].startswith("http")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    asyncio.run(with_client(scenario))


def test_kaggle_code_download_is_available_in_local_demo() -> None:
    async def scenario(client: httpx.AsyncClient) -> None:
        response = await client.get("/api/kaggle-code")
        assert response.status_code == 200
        assert "BacktestConfig" in response.text
    asyncio.run(with_client(scenario))
