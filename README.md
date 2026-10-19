# Quant Desk — Kaggle CPU Research + Alpaca Paper

一个中文、单用户、移动端优先的美股量化研究与 Paper 仪表板。Kaggle 离线研究、Demo 演示和 Alpaca Paper 运行分开；Paper 与 Demo 共用一套前端组件。

> **风险提示：**本项目用于研究与模拟交易，不构成投资建议。Demo 数字是合成演示数据；历史回测与 Alpaca Paper 均不代表真实成交或未来表现。策略可能亏损。系统不会连接 Alpaca Live Trading API。

## 当前状态与安全边界

- 已实现 FastAPI 后端、React/Vite 前端、离线 CSV 回测、测试套件、Compose 与 Cloudflare Tunnel sidecar 配置。
- 本地默认是 Demo。Alpaca 凭证、生产域名、服务器部署与真实 Paper API 尚未验证；不得把 mock/离线测试说成真实券商集成测试。
- 默认观察清单：`SPY, QQQ, AAPL, MSFT, NVDA`；策略是只做多 SMA 20/50、5 分钟 bar、常规交易时段、已完成 bar、隔夜持仓。
- Paper 策略限额：目标初始权益 $100,000、每标的最多 10%、总持仓最多 50%、无空头/杠杆；每日回撤达 2% 暂停新订单；自动市场单仅限 Paper endpoint。
- 行情使用 Alpaca IEX 单一交易所数据，不代表全市场 consolidated tape。
- 生产凭证只保存在服务器未跟踪的 `.env` 文件；永远不要提交 API key/secret、应用口令、Session secret 或 Cloudflare Tunnel token。
- 登录使用 HttpOnly 签名 Session、同源 CSRF 校验；单 IP 15 分钟内 5 次失败后临时限流。

完整的逐项决策见 [`MEMO.md`](MEMO.md)。

## 仓库结构

```text
frontend/       响应式中文 Web 仪表板（Paper / Demo 共用）
backend/        FastAPI、登录/CSRF、SQLite、Paper 状态机、Alpaca 适配器
shared/         CSV 规范化、SMA 信号、回测与风险指标
kaggle/         可上传至 Kaggle 的 Python 脚本与 .ipynb
scripts/        Kaggle deliverable 生成器
infra/          Cloudflare Tunnel 与部署说明
MEMO.md         规格、审查、测试和未验证事项
```

## 本地开发

需要 Python 3.11+、Node.js 20+ 和 npm。终端 1：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt -r backend/requirements.txt
python -m pytest -q
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
```

终端 2：

```bash
cd frontend
npm ci
npm run dev -- --host 0.0.0.0
```

打开 Vite 显示的地址。开发模式下，无口令 Demo 有本地快捷登录；这只适用于本机开发。生产 Demo 也必须设置应用口令和唯一 Session secret。不要把本机开发服务器直接暴露到公网。

## Kaggle CPU 回测

1. 将 `kaggle/quant_research.ipynb` 导入 Kaggle Notebook，启用 CPU 环境。
2. 添加数据集 [`olegshpagin/usa-stocks-prices-ohlcv`](https://www.kaggle.com/datasets/olegshpagin/usa-stocks-prices-ohlcv)。代码只扫描观察清单中可识别的 M5/5m CSV，不会下载或提交 11 GB 原始数据。
3. 检查 Notebook 报告的实际文件、标的和时间范围。若 CSV 时间戳无时区，**先查证数据集的时区说明/原始样例，再设置 `SOURCE_TIMEZONE`**；未知时区时程序会拒绝推测。时区/夏令时边界不明确时不要将结果当作可靠回测。
4. 输出前 70% 时间开发区间与后 30% 留出区间、同区间 Buy & Hold 对照、交易/权益 CSV 与图表。缺少的 ETF/股票直接跳过，不伪造数据。Buy & Hold 与策略使用相同的 50% 总持仓上限；回测滑点默认每边 5 bps，佣金默认 $0。
5. 不要把合成 fixture、Demo 曲线或短样本年化结果描述成真实绩效。短样本不计算年化收益；日度风险指标基于每日最后一个标记权益。

本地更新 Notebook / Python 交付文件：

```bash
.venv/bin/python scripts/build_kaggle_artifacts.py
```

## 部署

新手部署步骤（Linux 服务器、Docker Compose、Cloudflare Tunnel、口令、Paper 模式）见 [`infra/DEPLOYMENT.md`](infra/DEPLOYMENT.md)。Compose 不开放公网源站端口，Tunnel 连接至容器内网站服务。部署前先验证 Demo；缺少服务器、域名和 Alpaca Paper 凭证时，不会宣称部署或真实交易已完成。

## 验证

```bash
.venv/bin/python -m pytest -q
(cd frontend && npm run build)
```

测试包括时间戳/时区与夏令时处理、RTH 数据规范化、SMA 无未来数据、开盘成交与滑点、同时多标的风险上限、50% Buy & Hold 基准、按时间切分、Kaggle CPU fixture 端到端运行、登录/CSRF、幂等 Paper 下单、重启对账与每日熔断。Kaggle fixture 是专门用于自动化测试的合成数据，不是市场绩效。
