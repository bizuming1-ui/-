# 项目备忘录：Quant / Paper Trading Dashboard

记录日期：2026-10-19
状态：需求基线 v1，项目尚无既有业务源码；此前仓库仅有 3 字节 README。以下内容是用户逐项确认的实现约束，不是投资建议。

## 已确认范围

- 面向个人的美股/ETF 量化研究与模拟盘仪表板；中文 UI，金额以 USD 展示。
- 单一 Web 应用提供 Paper、Demo/Preview 两种数据模式，共用相同页面和组件。
- Paper 模式只允许 Alpaca Paper API。绝不连接 Live 下单端点；前端、Notebook 和 GitHub 中不得出现 API key/secret。
- 站点部署在用户自己的常开服务器；Cloudflare 域名/Tunnel 提供 HTTPS 入口。实际公网 URL 尚未提供，二维码使用 `APP_PUBLIC_URL` 配置。
- 用户不熟悉服务器操作；交付中须提供逐步安装、启动、更新、恢复和 Cloudflare Tunnel 配置说明。不能承诺服务器关机/断网时网站仍可用，也不能保证永久在线。

## 策略与风险参数

- 策略：只做多的简单移动均线交叉；5 分钟 bar，SMA 20 / SMA 50。
- 仅使用美东时间常规时段 09:30–16:00 产生信号；仓位允许隔夜持有。
- 信号只用已完成 bar。快线上穿慢线买入、下穿卖出；不增加未经确认的止盈/止损。
- Watchlist 初始值：SPY、QQQ、AAPL、MSFT、NVDA；配置可改。
- Paper 初始权益目标 $100,000；每个标的最多占权益 10%，总仓位上限 50%，不做空、不加杠杆。
- 自动提交 Paper 市价单；只在 bar 完成后下单，以 Alpaca 回报/实际账户持仓为准，不把未成交订单假定为持仓。
- 当日账户权益回撤达到 2% 时暂停新订单，人工确认后恢复。
- 服务重启后自动恢复策略意图，但必须先向 Paper 账户对账；对账失败则暂停并告警。停机期间的旧信号不追补，只从下一根完整 bar 继续。
- 订单写入幂等键/本地状态，避免重复提交；异常、过期数据、账户状态不一致时 fail closed。
- Kaggle 历史回测默认每边滑点 5 bps、佣金 $0，参数醒目可配置。回测成交模型与 Alpaca Paper 成交模型分开展示，不宣称等同真实市场。

## Kaggle 与数据

- Kaggle CPU Notebook 负责离线研究/回测，不存储或使用 Alpaca 交易密钥。
- 数据集候选已选：Kaggle `olegshpagin/usa-stocks-prices-ohlcv` 的 M5 文件。只读取可用标的，不将约 11 GB 原始数据放入 Git；数据页面显示样例截至 2024-03，需在 Notebook 输出中标注数据的实际时间范围、缺失标的和数据质量。
- 预期候选个股 AAPL/MSFT/NVDA；SPY/QQQ 如数据缺失必须跳过，不伪造结果。
- 数据按时间顺序 70/30 切分，禁止随机打乱；验证段不用于调参。与同一标的/同一时间段的 Buy & Hold 比较。
- 交付自包含 Python 脚本及有效 `.ipynb`；网站提供完整代码复制按钮和 Notebook 下载。
- 合成样本只用于 smoke test，必须明确标记，不得当作真实回测绩效。

## 安全与运维

- 单用户应用内登录；口令由服务器环境变量注入，Session Cookie 使用 HttpOnly、Secure（HTTPS 部署）、SameSite。敏感配置只保存在服务器环境文件，不提交 Git。
- 默认 Demo，不配置密钥时绝不尝试连接券商；启用 Paper 必须显式设置模式并校验 Paper endpoint。
- 生产环境只允许经 Cloudflare Tunnel/HTTPS 访问；CORS 同源；不在响应或日志中输出密钥。
- 数据/策略状态需持久化，重启时恢复后先对账；明确显示行情源为 Alpaca IEX（不是全市场 consolidated tape）。
- 必须有全局暂停/停止新订单控件、服务健康状态、行情最后更新时间、Paper/Demo 显著状态标识和免责声明。

## 设计与验收方向

- 手机优先、响应式、中文界面；高对比的暗色金融终端视觉、明确视觉层级与充分留白；图表、交互状态和空/错误/断线状态完整。
- 触屏点击有轻量反馈；遵守 `prefers-reduced-motion`；不以动画掩盖风险/连接状态。
- 页面提供同源的预览模式；Paper 和 Demo 共用渲染组件，Demo 使用清楚标注的静态/确定性样例数据。
- QR 只由 `APP_PUBLIC_URL` 生成；未部署前不能声称二维码可访问公网。
- 三轮实现后复查：策略/风险边界；安全/并发/恢复；响应式/UI/部署/文档。每轮先列发现与建议，再实施修复。
- 测试目标：策略单测与边界测试、无未来数据泄漏检查、CSV 校验、risk-limit/订单幂等模拟、Paper client mock、前端构建与 lint、健康接口；集成测试若没有真实 Paper credentials，只能标为 mock 验证，不能伪称实盘连通。

## 尚待部署时填写（不可在聊天发送密钥）

- `APP_PUBLIC_URL`：最终 HTTPS 域名
- `APP_PASSWORD`、`SESSION_SECRET`
- Alpaca Paper API key/secret（仅服务器环境变量）
- 服务器 OS/Docker 情况，以及 Cloudflare Tunnel hostname/route

## 复查记录

### Review 1 — 数学/数据、Demo 一致性、部署与基础安全（2026-10-19）

**发现并修复：**

- 5 分钟 bar 上直接年化会夸大风险指标；现在先按美东交易日取日末权益，252 年化，样本不足 20 个日度收益时不展示 CAGR，并报告已平仓往返交易的胜率/Profit Factor。
- 多标的同一时间买入时，组合风控原先可能用其他标的过期收盘价；现在用该时点可用开盘价计价，并加入同时买入测试。Buy & Hold 基准也按同一 50% 总敞口、滑点和佣金约束。
- Kaggle 数据的时区仍未核实；默认不再猜 `America/New_York`。时间戳带偏移时按 UTC 处理（覆盖 DST 混合偏移）；无时区时必须明确设置来源时区；混合 naive/aware 会报错。Kaggle 真实数据尚未下载/运行。
- Demo 账户现金、持仓成本/市值、权益曲线、未实现盈亏、基准末值现在互相一致；仅使用合成演示标签。Paper 模式不再用 Demo 曲线或虚构 Sharpe 填空；账户快照无历史曲线时清楚显示无数据，收益区分于策略回测。
- Paper 订单列表只显示已提交并取得券商提交回报的订单，携带数量/订单 ID/状态；非订单信号或被风控阻止的事件不再伪装成订单。持仓继续取账户 API 快照。
- Compose 新增 token 驱动的 `cloudflared` sidecar、服务器端 Tunnel route 说明、强生产 HTTPS/口令校验、CSP/HSTS/安全响应头、登录失败限流、构建上下文 secret 排除与新手部署步骤。

**复测：**29 项 Python 测试通过（含合成 Kaggle CSV CPU 端到端 fixture）；Vite/TypeScript production build 通过，代码分块无 oversized-chunk 警告；`git diff --check` 通过。

**保留的限制：**没有真实 Kaggle 时间戳约定或 CSV 统计可核实；Docker CLI 不在当前环境，因此未做 Compose config/build；没有 Alpaca Paper 凭证，因此券商 API/订单仍为 mock-only；无法在没有服务器/域名配置的情况下验证 Cloudflare 公网 Tunnel。Paper Equity 历史图尚未持久化，所以该模式诚实展示账户快照而不画假曲线。


## 新增：深圳 ETF 159502 单标的纸面模拟器（2026-10-19）

> 仅面向纸面模拟与研究，不构成投资建议。所有成本为可配置占位值，非真实费率；离线/Demo 需醒目标注。

**标的：** 嘉实标普生物科技精选行业 ETF（QDII）159502.SZ · SZSE · 时区 Asia/Shanghai · 交易时段 09:30-11:30 / 13:00-15:00 CST · 允许隔夜持仓

**策略：** 5 分钟 K 线 SMA 20/50、只做多、已完成 bar 触发、下一根可交易 K 开盘成交（整股向下取整）

**资金与风控：** 初始虚拟资金 ¥100,000（CNY）；单笔仓位上限权益 10%；日回撤达 2% 后停止新买入并尝试卖出可卖持仓（SELL 仍放行，BUY 拦截），需人工确认后方可恢复；成本（滑点 bps / 佣金）仅为占位参数，前端与后端均醒目标注“非真实费率”

**同花顺对接（受限）：**
- 用户在 Playground 手动查询 /api/fund/market/snapshot?thscode=159502.SZ 返回 code=0（已记录），但费用、额度、授权范围尚未确认
- 实现约束：不发起额外真实 API 调用；实时轮询默认关闭；若实现接口，后端仅从服务端环境变量 TONGHUASHUN_API_KEY 读取 Key，前端/浏览器存储/Notebook/GitHub/聊天均不得保存
- 历史基金接口为日线，高频分钟接口不应被假装为可用；没有已授权的 159502 五分钟历史数据时，不做真实数据回测——当前仅提供合成离线 Demo 曲线与快照，明确标注非实时

**离线/Demo 标注：**
- 所有 159502 页面、接口、图表均标注“离线 Demo · 合成数据 · 非实时行情”
- 未获授权 5 分钟历史时，bars 接口返回 {available:false, reason}，不伪造 K 线
- 成本卡片统一显示“成本为可配置占位值，非真实费率，仅用于纸面演示”

**未验证事项（需部署/授权后核实）：**
- 同花顺费用、额度、QPS、授权范围与分钟级历史接口真实可用性
- 深圳 QDII 是否存在停牌/换汇/节假日特殊时段差异
- 真实 159502 盘口最小变动单位与整股规则在纸面成交模型中的精确性
- TONGHUASHUN_API_KEY 的轮转、权限与审计日志方案

**已实现：**
- 后端 Settings 新增 sz159502_* 与 tonghuashun_* 字段，校验占位成本与轮询开关；Key 仅环境变量来源
- shared/quant_core 新增 SZ_TZ、SZ_SESSION 与 normalize_sz_ohlcv、run_sz159502_backtest（未授权时抛错不回测）与 demo_sz159502_equity_curve
- 后端 tonghuashun_market 适配器（离线优先、脱敏状态、demo_snapshot、latest_5min_bars 占位）、sz_portfolio 虚拟账户（CNY）与 sz159502_engine（日熔断止买放卖、下一根开盘成交、允许隔夜）
- 新增 API：/api/sz159502/status、/snapshot、/bars-note、/demo-dashboard、/engine/start|stop|status，Health 扩展 sz159502 字段
- 前端新增“159502 纸面”独立页面、总览关联、策略页与设置页的 159502 风控/成本/未验证说明；金额按 CNY/CNY 格式化；所有成本标注占位

### Review 1 补充复查 — 159502 增量（2026-10-19）

**发现并修复：**
- 新增 Shenzhen 时段过滤，修正美股 09:30-16:00 与深圳 09:30-11:30/13:00-15:00 的 RTH 误用风险
- 日熔断由“暂停全部”改为 159502 侧“止新买、放卖出”，符合用户要求的尝试平仓语义；US 侧保持原有暂停语义以兼容既有测试
- 将成本统一收口为占位参数，前端卡片与后端状态均标注非真实费率
- 加入授权门控：无 5 分钟历史授权时 run_sz159502_backtest 拒绝回测，返回明确错误而非伪造曲线

**保留的限制：**
- 仍无 159502 真实五分钟历史样本；Kaggle 侧仍仅支持美股 M5 数据集
- 未在真实同花顺网关上验证分钟级接口路径与额度；当前分钟历史保持占位空数据
- 未在服务器上验证 Cloudflare Tunnel 与真实域名下的 159502 轮询链路（轮询默认关闭）

### Review 2 与 Review 3

尚未执行。每一轮都须另行列出发现与修复，并明确保留上述不可验证边界。

## 明确不承诺

- 不承诺盈利、零风险、零 bug、永久免费或永远在线。
- Kaggle 历史回测、Alpaca Paper 和真实交易成交表现不等价。
- 未确认数据许可前不将外部原始行情镜像进 GitHub 或公开再分发。
