"""08 一般做法版：服务端保存会话 + Cookie + 访问口令 + 限流 + 历史裁剪。

和第 7 步的区别（第 7 步是"最小版"，看懂原理用；这一版是"接近可用"）：

| 项目 | 07 最小版 | 08 一般做法 |
|---|---|---|
| 对话历史 | 浏览器存，每次全发过来 | 服务端存，浏览器只发这一句话 |
| 用户识别 | 无 | Cookie 里的 session id |
| 谁能用 | 谁都能用 | 可配访问口令，没口令本机可用 |
| 防刷 | 无 | 每会话每分钟上限 |
| 长对话 | 无限增长，会撞上限 | 只带最近 N 条 |

跑起来：
    python 08_app.py                 # http://127.0.0.1:8020
    python 08_app.py --port 9000

可选环境变量（写在 .env 里，或命令行临时设）：
    ACCESS_TOKEN=你的口令        设置后，页面要先输入口令才能用
    RATE_LIMIT_PER_MIN=20        每会话每分钟请求上限
    MAX_HISTORY=12               每次发给模型的历史消息条数上限
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
import uuid
from collections import deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import MODEL, configure_stdout, explain_error, get_client, load_env

ROOT = Path(__file__).parent
PAGE = ROOT / "static" / "app.html"

SYSTEM_PROMPT = "你是一个说话简洁的中文助手，回答尽量短。"

SESSION_IDLE_SECONDS = 3600  # 一小时没动就回收
COOKIE_NAME = "practice_session"


def int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# 会话：一般做法里，状态放在服务端，客户端只拿一个 id
# ---------------------------------------------------------------------------

class Session:
    def __init__(self) -> None:
        self.id = uuid.uuid4().hex
        self.messages: list[dict] = []
        self.created = time.time()
        self.last_seen = time.time()
        self.hits: deque[float] = deque()   # 最近一分钟的请求时间戳
        self.lock = threading.Lock()        # 同一会话串行处理，避免历史错乱


SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()


def get_session(session_id: str | None) -> Session:
    """拿到会话；没有就新建。

    同时顺手回收闲置会话——真实项目里这一步应该交给 Redis 的过期键，
    这里用最朴素的方式表达同一个意思。
    """
    now = time.time()
    with SESSIONS_LOCK:
        for key, sess in list(SESSIONS.items()):
            if now - sess.last_seen > SESSION_IDLE_SECONDS:
                SESSIONS.pop(key, None)

        if session_id and session_id in SESSIONS:
            sess = SESSIONS[session_id]
            sess.last_seen = now
            return sess

        sess = Session()
        SESSIONS[sess.id] = sess
        return sess


def check_rate_limit(sess: Session, limit: int) -> int:
    """返回还需等待的秒数，0 表示放行。"""
    now = time.time()
    while sess.hits and now - sess.hits[0] > 60:
        sess.hits.popleft()
    if len(sess.hits) >= limit:
        return max(1, int(60 - (now - sess.hits[0])))
    sess.hits.append(now)
    return 0


# ---------------------------------------------------------------------------
# 模型调用
# ---------------------------------------------------------------------------

def history_window(messages: list[dict], limit: int) -> list[dict]:
    """只取最近 limit 条，并保证第一条是 user。

    为什么必须从 user 开始：截断点如果落在 assistant 的回答中间，
    模型会收到一个"没有提问的回答"，接话会莫名其妙。
    """
    if limit <= 0:
        return []
    window = messages[-limit:]
    while window and window[0].get("role") != "user":
        window = window[1:]
    return window


def stream_reply(messages: list[dict]):
    client = get_client(timeout=120.0, max_retries=0)
    stream = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
        extra_body={"thinking": {"type": "disabled"}},
    )
    for chunk in stream:
        if chunk.usage:
            yield {"usage": {"in": chunk.usage.prompt_tokens, "out": chunk.usage.completion_tokens}}
        if not chunk.choices:
            continue
        text = chunk.choices[0].delta.content
        if text:
            yield {"delta": text}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "PracticeApp/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[http] {self.address_string()} {fmt % args}")

    # --- 小工具 ---

    def send_json(self, status: int, payload: dict, cookie_header: str | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie_header:
            self.send_header("Set-Cookie", cookie_header)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("请求体是空的")
        if length > 64 * 1024:
            raise ValueError("请求体太大")
        return json.loads(self.rfile.read(length))

    def session_from_cookie(self) -> tuple[Session, str | None]:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        sid = cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else None
        sess = get_session(sid)
        set_cookie = None
        if sid != sess.id:
            # HttpOnly：JS 读不到，降低 XSS 偷走会话的风险
            set_cookie = (
                f"{COOKIE_NAME}={sess.id}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_IDLE_SECONDS}"
            )
        return sess, set_cookie

    def require_token(self) -> bool:
        """配了口令就校验，没配就提醒一句（本机练习用）。"""
        expected = os.environ.get("ACCESS_TOKEN", "").strip()
        if not expected:
            return True
        got = self.headers.get("X-Access-Token", "")
        return secrets.compare_digest(got, expected)

    # --- 路由 ---

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/me":
            sess, set_cookie = self.session_from_cookie()
            self.send_json(
                200,
                {
                    "auth_required": bool(os.environ.get("ACCESS_TOKEN", "").strip()),
                    "history": len(sess.messages),
                    "model": MODEL,
                },
                set_cookie,
            )
            return

        self.send_error(404, "只有 GET / , GET /api/me, POST /chat, POST /reset")

    def do_POST(self) -> None:
        if self.path not in ("/chat", "/reset"):
            self.send_error(404, "只有 GET / , GET /api/me, POST /chat, POST /reset")
            return

        if not self.require_token():
            self.send_json(401, {"error": "需要访问口令（页面右上角输入）"})
            return

        sess, set_cookie = self.session_from_cookie()

        if self.path == "/reset":
            with sess.lock:
                sess.messages.clear()
            self.send_json(200, {"ok": True, "history": 0}, set_cookie)
            return

        try:
            payload = self.read_json()
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip():
                raise ValueError("message 必须是非空字符串")
            message = message.strip()[:4000]  # 限制单条长度，防止灌爆上下文
        except Exception as exc:
            self.send_json(400, {"error": f"请求格式不对：{exc}"}, set_cookie)
            return

        limit = int_env("RATE_LIMIT_PER_MIN", 20)
        wait = check_rate_limit(sess, limit)
        if wait:
            self.send_json(429, {"error": f"太频繁了，{wait} 秒后再试"}, set_cookie)
            return

        # SSE 响应头
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()

        with sess.lock:
            sess.messages.append({"role": "user", "content": message})

            max_history = int_env("MAX_HISTORY", 12)
            # 只带最近 N 条：长对话必须有截断策略，否则成本和延迟都会失控
            window = history_window(sess.messages, max_history)
            request_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + window

            answer = ""
            try:
                for event in stream_reply(request_messages):
                    if event.get("delta"):
                        answer += event["delta"]
                    self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                print("[http] 客户端提前断开")
                return
            except Exception as exc:
                # 一个字都没生成，就把这轮用户消息撤回，别留下没有回答的问题
                if not answer:
                    sess.messages.pop()
                error = {"error": explain_error(exc)}
                try:
                    self.wfile.write(f"data: {json.dumps(error, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    return
                return

            sess.messages.append({"role": "assistant", "content": answer})

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> int:
    configure_stdout()
    load_env()

    port = 8020
    if "--port" in sys.argv:
        index = sys.argv.index("--port")
        if index + 1 < len(sys.argv):
            port = int(sys.argv[index + 1])

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    print(f"服务已启动：http://127.0.0.1:{port}")
    print(f"模型 {MODEL}")
    print(f"访问口令：{'已启用' if os.environ.get('ACCESS_TOKEN', '').strip() else '未设置（谁打开都能用，仅限本机）'}")
    print(f"限流：每分钟 {int_env('RATE_LIMIT_PER_MIN', 20)} 次 / 历史窗口 {int_env('MAX_HISTORY', 12)} 条")
    print("按 Ctrl+C 停止")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
