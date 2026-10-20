from __future__ import annotations
"""
JoinQuant 适配占位（离线 Demo，不发起真实调用）
第一性原理：
- 历史数据：日线授权明确，分钟线需额度/许可，未获授权不做真实回测
- 行情：Go 网关并发拉取预留，Python 侧仅聚合
- 安全：JOINQUANT_API_KEY 仅服务端环境变量，前端/Notebook/GitHub绝不保存
"""
from typing import Any
from backend.app.settings import Settings

def joinquant_status(settings: Settings) -> dict[str, Any]:
    return {
        "enabled": False,
        "mode": "offline_demo",
        "note": "JoinQuant 未接入真实调用（需 JOINQUANT_API_KEY + 数据许可）；当前离线合成演示",
        "has_authorized_5min_history": False,
        "data_granularity": "日线/分钟线待授权",
        "security": "JOINQUANT_API_KEY 仅服务端环境变量",
    }
