"""09 工单助手：把抽取、工具调用、流式生成串成一个能用的东西。

一次请求做三件事：
    1. 抽取   把口语化的客户留言变成结构化字段（JSON 模式）
    2. 查证   提到订单号就调用工具查物流状态（工具调用）
    3. 起草   结合字段和查证结果，流式写一段客服回复草稿

跑起来：
    python 09_ticket_assistant.py             # http://127.0.0.1:8030
    ACCESS_TOKEN=口令 python 09_ticket_assistant.py

接口：
    GET  /          页面
    POST /triage    {"message": "..."}  ->  text/event-stream
        {"stage": "extract"}          开始抽取
        {"fields": {...}}             抽取结果
        {"tool": {...}}               工具调用与返回
        {"stage": "draft"}            开始写草稿
        {"delta": "..."}              草稿增量
        {"usage": {...}}              token 用量
        data: [DONE]                  结束

和 08 的区别：这一版没有多轮会话，每次请求都是一次性的"处理一单"。
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from openai import OpenAI

from common import MODEL, configure_stdout, explain_error, load_env, require_key

ROOT = Path(__file__).parent
PAGE = ROOT / "static" / "ticket.html"

EXTRACT_SYSTEM = "你是信息抽取器。只输出 JSON 对象，不输出解释，不加代码块标记。"

EXTRACT_TEMPLATE = """把下面这段客户留言抽取成 JSON，结构如下：

{{
  "customer": 姓名或 null,
  "contact": 电话或 null（只保留数字）,
  "order_id": 订单号或 null,
  "product": 商品或 null,
  "issue": 一句话概括问题或 null,
  "urgency": "low" 或 "medium" 或 "high",
  "wants_refund": true 或 false
}}

规则：
1. 原文没有的信息填 null，不要编造。
2. 只输出这一个 JSON 对象。
3. urgency 只能从 low / medium / high 里选。
4. wants_refund 必须是布尔值。

留言：
{text}"""

AGENT_SYSTEM = (
    "你是电商客服助手。只依据已知的工单信息和工具返回的结果作答，"
    "不要编造物流进度，不要承诺赔付时效。"
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_order_status",
            "description": "按订单号查询物流状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "订单号，例如 A20261015001"},
                },
                "required": ["order_id"],
            },
        },
    }
]


def get_order_status(order_id: str) -> str:
    """示例数据。真实项目里这里应该查订单库或物流接口。"""
    return json.dumps(
        {
            "order_id": order_id,
            "status": "已打包待揽收",
            "carrier": "顺丰",
            "updated": "今天 09:20",
        },
        ensure_ascii=False,
    )


TOOL_IMPL = {"get_order_status": get_order_status}

FIELDS = ("customer", "contact", "order_id", "product", "issue", "urgency", "wants_refund")


def validate_fields(data) -> list[str]:
    if not isinstance(data, dict):
        return ["抽取结果不是 JSON 对象"]
    problems = []
    for key in FIELDS:
        if key not in data:
            problems.append(f"缺少字段 {key}")
    for key in ("customer", "contact", "order_id", "product", "issue"):
        value = data.get(key)
        if key in data and not isinstance(value, (str, type(None))):
            problems.append(f"{key} 应该是字符串或 null")
    if data.get("urgency") not in ("low", "medium", "high"):
        problems.append(f"urgency 不合法：{data.get('urgency')!r}")
    if not isinstance(data.get("wants_refund"), bool):
        problems.append("wants_refund 必须是布尔值")
    return problems


# --- 限流：按来源地址，简单够用 ------------------------------------------------

HITS: dict[str, list[float]] = {}
HITS_LOCK = threading.Lock()


def allowed(client_addr: str, limit: int) -> bool:
    now = time.time()
    with HITS_LOCK:
        bucket = HITS.setdefault(client_addr, [])
        while bucket and now - bucket[0] > 60:
            bucket.pop(0)
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


def int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "TicketAssistant/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[http] {self.address_string()} {fmt % args}")

    # --- 工具 ---

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def emit(self, payload: dict) -> None:
        self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def check_token(self) -> bool:
        expected = os.environ.get("ACCESS_TOKEN", "").strip()
        if not expected:
            return True
        return secrets.compare_digest(self.headers.get("X-Access-Token", ""), expected)

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
        self.send_error(404, "只有 GET / 和 POST /triage")

    def do_POST(self) -> None:
        if self.path != "/triage":
            self.send_error(404, "只有 GET / 和 POST /triage")
            return

        if not self.check_token():
            self.send_json(401, {"error": "需要访问口令"})
            return

        if not allowed(self.client_address[0], int_env("RATE_LIMIT_PER_MIN", 10)):
            self.send_json(429, {"error": "太频繁了，等一分钟再试"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length))
            text = payload.get("message")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("message 必须是非空字符串")
            text = text.strip()[:4000]
        except Exception as exc:
            self.send_json(400, {"error": f"请求格式不对：{exc}"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        try:
            self.handle_triage(text)
        except (BrokenPipeError, ConnectionResetError):
            print("[http] 客户端提前断开")
            return
        except Exception as exc:
            try:
                self.emit({"error": explain_error(exc)})
            except Exception:
                return

        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # --- 主体流程 ---

    def handle_triage(self, text: str) -> None:
        client = OpenAI(api_key=require_key(), base_url="https://api.deepseek.com", timeout=120.0, max_retries=1)

        # 第一段：抽取
        self.emit({"stage": "extract"})
        extraction = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": EXTRACT_TEMPLATE.format(text=text)},
            ],
            response_format={"type": "json_object"},
            max_tokens=600,
            extra_body={"thinking": {"type": "disabled"}},
        )
        raw = extraction.choices[0].message.content or ""
        try:
            fields = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.emit({"error": f"抽取结果不是合法 JSON：{exc.msg}"})
            return

        problems = validate_fields(fields)
        if problems:
            self.emit({"error": "抽取结果不合格：" + "；".join(problems), "raw": raw})
            return
        self.emit({"fields": fields})

        if extraction.usage:
            self.emit(
                {
                    "usage": {
                        "step": "extract",
                        "in": extraction.usage.prompt_tokens,
                        "out": extraction.usage.completion_tokens,
                    }
                }
            )

        # 第二段：需要时调用工具
        messages = [
            {"role": "system", "content": AGENT_SYSTEM},
            {
                "role": "user",
                "content": f"客户留言：{text}\n\n已抽取字段：{json.dumps(fields, ensure_ascii=False)}",
            },
        ]

        tool_notes: list[str] = []
        for _ in range(3):
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                extra_body={"thinking": {"type": "disabled"}},
            )
            message = response.choices[0].message
            if not message.tool_calls:
                break

            entry: dict = {"role": "assistant", "content": message.content}
            entry["tool_calls"] = [call.model_dump() for call in message.tool_calls]
            messages.append(entry)

            for call in message.tool_calls:
                name = call.function.name
                raw_args = call.function.arguments
                try:
                    result = TOOL_IMPL[name](**json.loads(raw_args))
                except Exception as exc:
                    result = json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
                self.emit({"tool": {"name": name, "args": raw_args, "result": result}})
                tool_notes.append(result)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

        # 第三段：流式写草稿（不再给工具，避免它继续查）
        self.emit({"stage": "draft"})
        messages.append(
            {
                "role": "user",
                "content": "请根据以上信息，写一段客服回复草稿：不超过 120 字，语气平和，"
                "说明当前状态与下一步，不要承诺具体时效。",
            }
        )

        stream = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            extra_body={"thinking": {"type": "disabled"}},
        )
        for chunk in stream:
            if chunk.usage:
                self.emit(
                    {
                        "usage": {
                            "step": "draft",
                            "in": chunk.usage.prompt_tokens,
                            "out": chunk.usage.completion_tokens,
                        }
                    }
                )
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                self.emit({"delta": delta})


def main() -> int:
    configure_stdout()
    load_env()

    port = 8030
    if "--port" in sys.argv:
        index = sys.argv.index("--port")
        if index + 1 < len(sys.argv):
            port = int(sys.argv[index + 1])

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"工单助手已启动：http://127.0.0.1:{port}")
    print(f"模型 {MODEL}｜口令：{'已启用' if os.environ.get('ACCESS_TOKEN', '').strip() else '未设置'}")
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
