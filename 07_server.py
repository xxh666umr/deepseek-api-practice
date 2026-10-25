"""07 最小 Web 后端：把流式对话包成 HTTP 接口，配一个网页直接聊。

为什么用标准库而不是 FastAPI：这一版零依赖，双击就能跑，而且能看清
"流式"在 HTTP 层到底是什么——就是服务端一点一点往同一个响应里写 data: 行。
（FastAPI 版见 README 末尾，写法更短，但要先 pip install fastapi uvicorn。）

用法：
    python 07_server.py            # 默认 8010 端口
    python 07_server.py --port 9000

然后浏览器打开 http://127.0.0.1:8010

接口只有一个：
    POST /chat
    请求 {"messages": [{"role": "user", "content": "你好"}]}
    响应 text/event-stream，每行 data: {...}
        {"delta": "文"}      正文增量
        {"usage": {...}}     token 用量
        {"error": "..."}     出错
        data: [DONE]         结束
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import MODEL, configure_stdout, explain_error, get_client, load_env

ROOT = Path(__file__).parent
INDEX_FILE = ROOT / "static" / "index.html"

SYSTEM_PROMPT = "你是一个说话简洁的中文助手，回答尽量短。"


def stream_reply(messages: list[dict]):
    """把模型吐的增量翻译成一个个事件 dict。"""
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
            yield {
                "usage": {
                    "in": chunk.usage.prompt_tokens,
                    "out": chunk.usage.completion_tokens,
                }
            }
        if not chunk.choices:
            continue
        text = chunk.choices[0].delta.content
        if text:
            yield {"delta": text}


class Handler(BaseHTTPRequestHandler):
    server_version = "PracticeServer/0.1"

    # 默认日志太吵，只留一行
    def log_message(self, fmt: str, *args) -> None:
        print(f"[http] {self.address_string()} {fmt % args}")

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            body = INDEX_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404, "只有 / 和 POST /chat")

    def _send_json_error(self, status: int, message: str) -> None:
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/chat":
            self.send_error(404, "只有 / 和 POST /chat")
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)

        # 先做输入校验：服务端永远不能相信客户端发来的东西
        try:
            payload = json.loads(raw)
            history = payload.get("messages") or []
            if not isinstance(history, list) or not history:
                raise ValueError("messages 必须是非空数组")
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            for item in history:
                role = item.get("role")
                content = item.get("content")
                if role not in ("user", "assistant") or not isinstance(content, str):
                    raise ValueError("每条消息需要 role(user/assistant) 和字符串 content")
                messages.append({"role": role, "content": content})
        except Exception as exc:
            self._send_json_error(400, f"请求格式不对：{exc}")
            return

        # SSE：一次响应，边算边写。注意不带 Content-Length，写完就关连接
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        try:
            for event in stream_reply(messages):
                self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 用户关了页面，没什么可做的，别让异常刷屏
            print("[http] 客户端提前断开")
            return
        except Exception as exc:
            error = {"error": explain_error(exc)}
            self.wfile.write(f"data: {json.dumps(error, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> int:
    configure_stdout()
    load_env()

    port = 8010
    if "--port" in sys.argv:
        index = sys.argv.index("--port")
        if index + 1 < len(sys.argv):
            port = int(sys.argv[index + 1])

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"服务已启动：http://127.0.0.1:{port}")
    print(f"模型 {MODEL}，按 Ctrl+C 停止")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
