# 初次部署：Linux 服务器 + Cloudflare Tunnel

> 本指南默认服务器为 Ubuntu 22.04/24.04 x86_64。若你的服务器是 Windows、NAS、其他发行版，先不要照抄安装命令；把系统名称发给我，我会按实际系统改写。部署完成前保持 `APP_MODE=demo`。此项目目前没有在真实服务器、Alpaca 账户或公开域名上运行验证。

## 0. 先确认服务器

在服务器终端运行 `cat /etc/os-release`、`uname -m`，并确认它会持续开机且能主动访问互联网。需要 Docker Engine 和 Docker Compose v2。Tunnel 使用服务器的出站连接；**不需要把服务器的 80/8000 端口暴露到公网，也不需要在路由器上做端口转发**。

安装 Docker 请按 Docker 官方 Ubuntu 文档的步骤进行：<https://docs.docker.com/engine/install/ubuntu/>。安装后运行：

```bash
docker --version
docker compose version
```

## 1. 将项目放到服务器

将本仓库克隆到服务器（若仓库为私有仓库，按 GitHub 的设备登录流程授权；不要把 GitHub token 粘贴到聊天或写进命令历史）：

```bash
git clone <本项目 GitHub 仓库地址> quant-desk
cd quant-desk
```

若代码尚未推送到 GitHub，应先在 Arena 完成推送，再进行这一步。不要下载原始 Kaggle 大数据集到服务器；Paper 服务不需要 Kaggle 数据。

## 2. 生成两个本机密钥文件

根目录 `.env` 只存 Cloudflare Tunnel token；`backend/.env` 存应用口令与服务端配置。两者均已被 Git 忽略：

```bash
cp .env.example .env
cp backend/.env.example backend/.env
chmod 600 .env backend/.env
openssl rand -hex 32
```

最后一条会生成随机值。将其填进 `backend/.env` 的 `SESSION_SECRET`；设置一个至少 16 字符且不重复使用的 `APP_PASSWORD`。不要把生成结果或真实 Tunnel token 发给任何人。使用编辑器打开文件（例如 `nano .env`、`nano backend/.env`），按下列建议设置：

- 根 `.env`：`CLOUDFLARE_TUNNEL_TOKEN=...`
- `backend/.env`：先设 `ENVIRONMENT=production`、`APP_MODE=demo`、`APP_PUBLIC_URL=https://你的子域名`、`SESSION_COOKIE_SECURE=true`、`DATABASE_PATH=/app/data/quant.db`。
- Demo 公开前必须配置 `APP_PASSWORD` 和独有 `SESSION_SECRET`；生产模式没有应用口令会拒绝启动。
- Paper 密钥此时保持空白。不要启用 Live Trading。

## 3. 建立 Cloudflare Tunnel

1. 登录 Cloudflare Zero Trust 控制台，进入 **Networks → Tunnels**（界面名称可能更新），创建一个命名 Tunnel，选择 Docker connector。
2. Cloudflare 会显示一条带有长 token 的运行命令。只复制 token 的值到服务器根目录 `.env` 的 `CLOUDFLARE_TUNNEL_TOKEN`，不要把整条命令或 token 放进 Git、截图或聊天。
3. 在该 Tunnel 的 **Public Hostnames** 添加主机名，例如 `quant.your-domain.com`；Service 类型选 HTTP，URL 填 `http://quant-web:80`。Compose 中 cloudflared 与网站处于同一个内部网络，因此使用服务名 `quant-web`；不要填 `localhost`。
4. 保存后，Cloudflare 会为该主机名设置路由。浏览器到 Cloudflare 的访问使用 HTTPS；源站只在 Compose 内网通过 HTTP 通信。
5. 把完全相同的 HTTPS 地址写入 `backend/.env` 的 `APP_PUBLIC_URL`，二维码才会指向正确域名。

## 4. 启动并检查

```bash
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 quant-api quant-web cloudflared
```

`cloudflared` 显示健康连接后，在浏览器打开 `APP_PUBLIC_URL`，以应用口令登录；再检查 `https://你的子域名/api/health`。Demo 必须显示为演示数据，不能被当成真实行情/收益。若失败，先查看上述日志；不要为“修复”而开放 8000 端口。

之后可用 `docker compose logs -f quant-api` 跟踪服务日志，按 `Ctrl+C` 退出日志查看（不会停止容器）。更新时先备份数据库，再拉取代码并运行 `docker compose up -d --build`。

## 5. 明确选择启用 Alpaca Paper（可选，先验证 Demo）

只有准备好之后才操作：在 Alpaca 创建/确认 **Paper** 账户，在 `backend/.env` 中设置 `APP_MODE=paper`、Paper API key/secret、`ALPACA_TRADING_URL=https://paper-api.alpaca.markets` 和 `ALPACA_DATA_FEED=iex`，然后重启 `docker compose up -d --build`。密钥只进服务器的 `backend/.env`。启动前会与 Paper 账户对账；异常状态将暂停策略。启动策略前检查账户、仓位、IEX 数据状态和风控说明。达到每日 2% 回撤限制后，新单会暂停；人工确认恢复会重置当日风险基准。Paper 是模拟撮合，不代表真实执行效果。

## 6. 运维边界

- 常开、联网的服务器是行情轮询与 Paper 引擎的运行条件；Cloudflare Tunnel 不会让关机的服务器继续交易。
- 当前 Compose 没有公网端口映射；保持主机防火墙关闭不需要的入站端口。
- SQLite 数据保存在 Docker named volume `quant_data`。定期备份此卷；不要在运行中直接复制数据库文件而不使用 SQLite 一致性备份方法。
- 先用 Demo 验证登录、HTTPS、二维码、备份和重启恢复，再考虑 Paper。没有真实账户凭证时只做 mock 测试，不声称券商集成已验证。
- 这里不配置真实账户/交易 Live API，也不保证网络、行情或策略始终可用。
