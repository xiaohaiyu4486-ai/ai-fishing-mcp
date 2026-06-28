#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎣 文字钓鱼游戏 · 远程 MCP server（纯标准库、零依赖）

把 fishing.py 的引擎包成一个可被 claude.ai / Claude Desktop 连接的 MCP 服务。
- 传输：streamable-http（POST /mcp）+ 旧版 SSE（GET /sse + POST /messages）
- 鉴权：Bearer token（MCP_AUTH_TOKEN），并实现 OAuth 2.0（register/authorize/token）
  好让 claude.ai 网页端能用「自定义连接器」直接连。
- 存档：写在持久磁盘上（DATA_DIR/fishing_save.json），重启不丢。

照搬自 co-reading-mcp 的 server-sse.js 的 OAuth/传输模式，单实例、个人使用足够。
"""
import os
import io
import json
import uuid
import time
import base64
import hashlib
import secrets
import threading
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ── 配置 ─────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", os.environ.get("MCP_SSE_PORT", "3100")))
HOST = os.environ.get("MCP_SSE_HOST", "0.0.0.0")
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
CORS_ORIGIN = os.environ.get("MCP_CORS_ORIGIN", "*" if AUTH_TOKEN else "")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ai-fishing-game"
SERVER_VERSION = "0.1.0"

os.makedirs(DATA_DIR, exist_ok=True)

# ── 接入游戏引擎（盲玩版，存档指到持久磁盘）────────────────────────────
import fishing  # noqa: E402  盲玩版：cmd() / new_game()，引擎被藏起来防剧透
fishing._SAVE = os.path.join(DATA_DIR, "fishing_save.json")
_GAME_LOCK = threading.Lock()  # 引擎状态是模块级全局，串行化所有调用

with open(os.path.join(HERE, "tool-schema.json"), "r", encoding="utf-8") as _f:
    _PLAY_SCHEMA = json.load(_f)

# ── 工具实现 ─────────────────────────────────────────────────────────
def _action_to_command(args):
    """把一组结构化参数转成一条引擎指令字符串（不执行）。"""
    a = (args or {}).get("action", "")
    if a in ("cast", "dive"):
        # cast [饵] [次数] [stop=...]；dive [氧气瓶数] [stop=...]（潜水不带饵）
        parts = [a]
        if a == "cast" and args.get("bait_id"):
            parts.append(str(args["bait_id"]))
        if args.get("times"):
            parts.append(str(int(args["times"])))
        if args.get("stop_on"):
            parts.append("stop=" + ",".join(args["stop_on"]))
        return " ".join(parts)
    if a == "choose":
        return f"choose {args.get('choice', '')}".strip()
    if a == "surface":
        return "surface"
    if a == "buy":
        # 买氧气瓶：bait_id='oxygen'
        return f"buy {args.get('bait_id', '')} {args.get('qty', 1)}".strip()
    if a == "goto":
        return f"goto {args.get('location_id', '')}".strip()
    if a == "sell":
        return f"sell {args.get('target', '')}".strip()
    if a == "open":
        return f"open {args.get('chest_uid', '')}".strip()
    if a == "look":
        return f"look {args.get('id', '')}".strip()
    # status / shop / inventory / encyclopedia / help …
    return a


def _play_fishing(args):
    """把结构化参数转成引擎指令字符串，再调 fishing.cmd()。"""
    a = (args or {}).get("action", "")
    if a == "batch":
        # 把多步排成队列，用 ; 串成一批一次执行（引擎原生支持 ;/换行批量）
        cmds = [_action_to_command(s) for s in (args.get("steps") or [])]
        return fishing.cmd("; ".join(c for c in cmds if c))
    return fishing.cmd(_action_to_command(args))


def _fishing_command(args):
    """直接执行一条原始指令字符串，如 'cast 10 stop=rare'。"""
    return fishing.cmd((args or {}).get("command", ""))


def _new_game(args):
    seed = (args or {}).get("seed")
    if seed in (None, ""):
        return fishing.new_game()
    return fishing.new_game(int(seed))


TOOLS = [
    {
        "name": "play_fishing",
        "description": _PLAY_SCHEMA["description"],
        "inputSchema": _PLAY_SCHEMA["parameters"],
        "_fn": _play_fishing,
    },
    {
        "name": "fishing_command",
        "description": (
            "直接执行一条钓鱼游戏的原始指令字符串（不想用结构化参数时用这个）。"
            "例：'cast 10 stop=rare'、'buy glow_bait 2'、'goto reed_river'、'status'、'help'。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "一条游戏指令，如 'cast 10 stop=rare'"}
            },
            "required": ["command"],
        },
        "_fn": _fishing_command,
    },
    {
        "name": "fishing_new_game",
        "description": "重开一局（会覆盖当前存档！可选 seed，同 seed+同指令序列结果完全一致）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "seed": {"type": "number", "description": "随机种子，不填=默认种子"}
            },
        },
        "_fn": _new_game,
    },
]
TOOL_MAP = {t["name"]: t for t in TOOLS}


# ── MCP JSON-RPC 分发 ────────────────────────────────────────────────
def _rpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _rpc_ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def handle_rpc(msg):
    """返回 dict 响应；若是通知（无需回复）返回 None。"""
    if isinstance(msg, list):
        # 批量请求
        out = [handle_rpc(m) for m in msg]
        return [r for r in out if r is not None] or None

    method = msg.get("method")
    rid = msg.get("id")

    if method == "initialize":
        client_pv = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        return _rpc_ok(rid, {
            "protocolVersion": client_pv,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None  # 通知，不回复

    if method == "ping":
        return _rpc_ok(rid, {})

    if method == "tools/list":
        listed = [{"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]} for t in TOOLS]
        return _rpc_ok(rid, {"tools": listed})

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOL_MAP.get(name)
        if not tool:
            return _rpc_error(rid, -32602, f"Unknown tool: {name}")
        try:
            with _GAME_LOCK:
                text = tool["_fn"](args)
        except Exception as e:  # 引擎自身已很稳，这里兜底
            return _rpc_ok(rid, {
                "content": [{"type": "text", "text": f"⚠️ 出错了：{e}"}],
                "isError": True,
            })
        return _rpc_ok(rid, {"content": [{"type": "text", "text": str(text)}]})

    if rid is None:
        return None  # 未知通知
    return _rpc_error(rid, -32601, f"Method not found: {method}")


# ── OAuth in-memory（单实例，个人服务足够）───────────────────────────
_registered_clients = {}     # client_id -> {secret, redirect_uris}
_auth_codes = {}             # code -> {client_id, redirect_uri, challenge, method, expires}


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


# ── HTTP handler ─────────────────────────────────────────────────────
_sse_sessions = {}  # sessionId -> queue.Queue


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静点
        pass

    # —— 基础工具 ——
    def _external_base(self):
        proto = self.headers.get("x-forwarded-proto") or "http"
        proto = proto.split(",")[0].strip()
        host = self.headers.get("x-forwarded-host") or self.headers.get("host") or f"{HOST}:{PORT}"
        return f"{proto}://{host}"

    def _set_cors(self):
        if not CORS_ORIGIN:
            return
        self.send_header("access-control-allow-origin", CORS_ORIGIN)
        self.send_header("access-control-allow-methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("access-control-allow-headers", "content-type, authorization, mcp-protocol-version, mcp-session-id")
        self.send_header("access-control-expose-headers", "mcp-protocol-version, www-authenticate, mcp-session-id")

    def _send_json(self, status, obj, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self._set_cors()
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, max_bytes=25_000_000):
        length = int(self.headers.get("content-length", 0) or 0)
        if length <= 0:
            return b""
        if length > max_bytes:
            raise ValueError("Body too large")
        return self.rfile.read(length)

    def _read_json(self):
        return json.loads(self._read_body().decode("utf-8"))

    def _read_form(self):
        data = self._read_body(65536).decode("utf-8")
        out = {}
        for k, v in parse_qs(data).items():
            out[k] = v[0] if len(v) == 1 else v
        return out

    def _authorized(self, qs):
        if not AUTH_TOKEN:
            return True
        if self.headers.get("authorization") == f"Bearer {AUTH_TOKEN}":
            return True
        if qs.get("token", [None])[0] == AUTH_TOKEN:
            return True
        return False

    def _send_unauthorized(self):
        meta = f"{self._external_base()}/.well-known/oauth-protected-resource/mcp"
        self.send_response(401)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("www-authenticate", f'Bearer resource_metadata="{meta}"')
        self._set_cors()
        body = json.dumps({"error": "Unauthorized"}).encode("utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # —— OPTIONS（CORS 预检）——
    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors()
        self.send_header("content-length", "0")
        self.end_headers()

    # —— GET ——
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        base = self._external_base()

        if path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
            return self._send_json(200, {
                "resource": f"{base}/mcp",
                "resource_name": "AI Fishing Game MCP",
                "resource_documentation": f"{base}/",
                "bearer_methods_supported": ["header"],
                "scopes_supported": ["mcp"],
                "authorization_servers": [base],
            })

        if path == "/.well-known/oauth-authorization-server":
            return self._send_json(200, {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": ["S256", "plain"],
                "scopes_supported": ["mcp"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
            })

        if path == "/authorize":
            client_id = qs.get("client_id", [""])[0]
            redirect_uri = qs.get("redirect_uri", [""])[0]
            state = qs.get("state", [""])[0]
            challenge = qs.get("code_challenge", [""])[0]
            method = qs.get("code_challenge_method", ["plain"])[0]
            if client_id not in _registered_clients:
                return self._send_json(400, {"error": "invalid_client"})
            if not redirect_uri:
                return self._send_json(400, {"error": "invalid_request", "error_description": "redirect_uri required"})
            code = secrets.token_hex(32)
            _auth_codes[code] = {
                "client_id": client_id, "redirect_uri": redirect_uri,
                "challenge": challenge, "method": method, "expires": time.time() + 300,
            }
            sep = "&" if "?" in redirect_uri else "?"
            location = f"{redirect_uri}{sep}code={code}"
            if state:
                location += f"&state={state}"
            self.send_response(302)
            self.send_header("location", location)
            self._set_cors()
            self.send_header("content-length", "0")
            self.end_headers()
            return

        if path == "/health":
            return self._send_json(200, {
                "status": "ok", "transport": "streamable-http+sse",
                "dataDir": DATA_DIR, "sessions": len(_sse_sessions),
                "auth": "enabled" if AUTH_TOKEN else "disabled",
                "tools": [t["name"] for t in TOOLS],
            })

        if path in ("/", ""):
            return self._send_json(200, {
                "name": "🎣 AI Fishing Game MCP",
                "endpoints": {"mcp": "/mcp", "sse": "/sse", "messages": "/messages?sessionId=<id>", "health": "/health"},
                "connect": "在 claude.ai 自定义连接器里填 " + base + "/mcp",
            })

        if path == "/mcp":
            self.send_response(405)
            self.send_header("allow", "POST")
            self.send_header("mcp-protocol-version", PROTOCOL_VERSION)
            self._set_cors()
            self.send_header("content-length", "0")
            self.end_headers()
            return

        if path == "/sse":
            if not self._authorized(qs):
                return self._send_unauthorized()
            return self._handle_sse()

        return self._send_json(404, {"error": "Not found"})

    # —— POST ——
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/register":
            try:
                body = self._read_json()
            except Exception:
                body = {}
            client_id = str(uuid.uuid4())
            client_secret = secrets.token_hex(32)
            _registered_clients[client_id] = {
                "secret": client_secret,
                "redirect_uris": body.get("redirect_uris", []),
            }
            return self._send_json(201, {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uris": body.get("redirect_uris", []),
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
            })

        if path == "/token":
            try:
                body = self._read_form()
            except Exception:
                body = {}
            if body.get("grant_type") != "authorization_code":
                return self._send_json(400, {"error": "unsupported_grant_type"})
            code = body.get("code", "")
            data = _auth_codes.pop(code, None)
            if not data or data["expires"] < time.time():
                return self._send_json(400, {"error": "invalid_grant"})
            if data["challenge"]:
                verifier = body.get("code_verifier", "")
                if not verifier:
                    return self._send_json(400, {"error": "invalid_grant", "error_description": "code_verifier required"})
                if data["method"] == "S256":
                    calc = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
                else:
                    calc = verifier
                if calc != data["challenge"]:
                    return self._send_json(400, {"error": "invalid_grant", "error_description": "PKCE verification failed"})
            # 单实例：所有客户端共享同一个 access_token（= AUTH_TOKEN）
            return self._send_json(200, {
                "access_token": AUTH_TOKEN or "public",
                "token_type": "Bearer", "expires_in": 86400, "scope": "mcp",
            })

        if path == "/mcp":
            if not self._authorized(qs):
                return self._send_unauthorized()
            try:
                msg = self._read_json()
            except Exception as e:
                return self._send_json(400, {"error": f"Invalid JSON: {e}"})
            try:
                resp = handle_rpc(msg)
            except Exception as e:
                rid = msg.get("id") if isinstance(msg, dict) else None
                return self._send_json(200, _rpc_error(rid, -32000, str(e)))
            if resp is None:
                # 纯通知：202 无内容
                self.send_response(202)
                self._set_cors()
                self.send_header("mcp-protocol-version", PROTOCOL_VERSION)
                self.send_header("content-length", "0")
                self.end_headers()
                return
            return self._send_json(200, resp, extra_headers={"mcp-protocol-version": PROTOCOL_VERSION})

        if path == "/messages":
            if not self._authorized(qs):
                return self._send_unauthorized()
            session_id = qs.get("sessionId", [""])[0]
            q = _sse_sessions.get(session_id)
            if q is None:
                return self._send_json(404, {"error": "Unknown or expired SSE session"})
            try:
                msg = self._read_json()
            except Exception as e:
                return self._send_json(400, {"error": f"Invalid JSON: {e}"})
            # 先 202 应答，再把结果推到 SSE 流
            self._send_json(202, {"accepted": True})
            try:
                resp = handle_rpc(msg)
            except Exception as e:
                rid = msg.get("id") if isinstance(msg, dict) else None
                resp = _rpc_error(rid, -32000, str(e))
            if resp is not None:
                q.put(resp)
            return

        return self._send_json(404, {"error": "Not found"})

    # —— SSE 长连接 ——
    def _handle_sse(self):
        session_id = str(uuid.uuid4())
        q = queue.Queue()
        _sse_sessions[session_id] = q
        try:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-cache, no-transform")
            self.send_header("connection", "keep-alive")
            self.send_header("x-accel-buffering", "no")
            self._set_cors()
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            endpoint = f"{self._external_base()}/messages?sessionId={session_id}"
            self.wfile.write(f"event: endpoint\ndata: {endpoint}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    item = q.get(timeout=25)
                    payload = json.dumps(item, ensure_ascii=False)
                    self.wfile.write(f"event: message\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            _sse_sessions.pop(session_id, None)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"🎣 AI Fishing Game MCP server on http://{HOST}:{PORT}")
    print(f"   MCP (streamable-http): POST /mcp")
    print(f"   MCP (SSE):             GET /sse + POST /messages")
    print(f"   Data dir: {DATA_DIR}")
    print(f"   Auth: {'enabled' if AUTH_TOKEN else 'DISABLED (set MCP_AUTH_TOKEN before exposing!)'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
