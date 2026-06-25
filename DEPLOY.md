# 🎣 把钓鱼游戏部署成远程 MCP（Render）

这个仓库在原版「文字钓鱼游戏」之外，加了一个 `server.py`：把游戏引擎包成一个
**远程 MCP server**，让 claude.ai 网页端 / Claude Desktop 能直接连上来玩。

- **零依赖**：`server.py` 和游戏引擎都只用 Python 标准库，不装任何包。
- **传输**：`POST /mcp`（streamable-http，claude.ai 主用）+ `GET /sse` & `POST /messages`（旧版 SSE）。
- **鉴权**：Bearer token + 完整 OAuth 2.0（register / authorize / token，支持 PKCE），
  这样 claude.ai 的「自定义连接器」能走 OAuth 直接连。
- **存档**：写在持久磁盘 `DATA_DIR/fishing_save.json`，重启不丢。盲玩版引擎（防剧透）。

## 工具

| 工具 | 说明 |
|---|---|
| `play_fishing` | 结构化参数玩：`action` = status/shop/buy/cast/goto/inventory/sell/open/encyclopedia/look |
| `fishing_command` | 直接传一条原始指令字符串，如 `cast 10 stop=rare` |
| `fishing_new_game` | 重开一局（会覆盖存档，可选 seed） |

## 在 Render 上部署

仓库里有 `render.yaml`（Blueprint），两种方式：

### 方式 A：Blueprint（推荐，一步到位）
1. Render 控制台 → **New → Blueprint** → 选这个 GitHub 仓库。
2. Render 读到 `render.yaml`，会建一个 Python web 服务 + 1GB 持久磁盘（挂在 `/var/data`）。
3. 部署时会让你填 `MCP_AUTH_TOKEN`（`sync:false` 的变量）——填一个随机长字符串。

### 方式 B：手动建 Web Service
1. **New → Web Service** → 选这个仓库。
2. 配置：
   - Runtime: **Python**
   - Build Command: 留空（零依赖）
   - Start Command: `python server.py`
3. **Environment** 里加：
   - `MCP_AUTH_TOKEN` = 一个随机长字符串（**务必设置**，否则服务裸奔）
   - `DATA_DIR` = `/var/data`
4. **Disks** 加一块盘，Mount Path = `/var/data`（存档落在这，重启不丢）。

> Render 会注入 `PORT`，`server.py` 已自动读取。

## 连到 claude.ai

1. claude.ai → Settings → Connectors → **Add custom connector**。
2. URL 填：`https://<你的服务名>.onrender.com/mcp`
3. 走 OAuth 授权（自动跳转、自动批准）。连上后工具前缀是
   `mcp__claude_ai_<连接器名>__play_fishing` 之类。

也可以用 Claude Desktop（`mcp-remote`）或带 `?token=<MCP_AUTH_TOKEN>` 直连。

## 本地跑 / 自测

```bash
MCP_AUTH_TOKEN=test PORT=3100 DATA_DIR=./data python3 server.py
curl -s --noproxy '*' http://127.0.0.1:3100/health
```

## 环境变量一览

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | 3100 | Render 自动注入 |
| `MCP_AUTH_TOKEN` | 空 | Bearer token；**生产务必设置** |
| `DATA_DIR` | `./data` | 存档目录，Render 上指向持久磁盘 |
| `MCP_CORS_ORIGIN` | 设了 token 则 `*` | CORS 来源 |

游戏玩法本身见 [`README.md`](./README.md)。
