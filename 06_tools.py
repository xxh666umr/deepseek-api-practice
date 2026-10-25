"""06 工具调用：让模型能"做事"，而不只是"说话"。

用法：
    python 06_tools.py            # 正常流程：模型自己决定查天气、查订单
    python 06_tools.py --break    # 复现一个真实的 400：thinking 模式不支持 tool_choice="required"

一次用户提问可能要走好几轮请求：

  第 1 轮：你发消息 → 模型说"我要调 get_order_status，参数是 {...}"
  第 2 轮：你本地执行函数，把结果作为 role=tool 的消息发回去 → 模型可能再申请别的工具
  第 3 轮：再执行、再回传 → 模型终于给出最终回答

记住：模型从来不执行你的代码，它只是"申请调用"。真正跑函数的是你自己，
所以参数要校验、要防注入、要限制它能碰到的数据范围。

关于 reasoning_content（思考内容）：官方文档要求带 tools 时把历史的思考内容原样回传，
否则报 400。实测当前版本漏传并不报错，但不回传等于让模型丢掉上一轮的推理过程。
这里选择按文档回传——跨版本更安全，模型也更容易接上前文。
"""

from __future__ import annotations

import datetime
import json
import sys

from openai import APIStatusError

from common import MODEL, configure_stdout, explain_error, get_client

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询某个城市某一天的天气",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "城市名，例如 杭州"},
                    "date": {"type": "string", "description": "日期，格式 YYYY-MM-DD"},
                },
                "required": ["location", "date"],
            },
        },
    },
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
    },
]


# --- 真正的函数，模型只会"申请调用"，执行永远发生在你这边 ---------------------

def get_weather(location: str, date: str) -> str:
    # 真实项目里这里应该去调气象 API，练习用固定数据
    return json.dumps(
        {"location": location, "date": date, "weather": "多云", "temp": "17~24℃"},
        ensure_ascii=False,
    )


def get_order_status(order_id: str) -> str:
    return json.dumps(
        {"order_id": order_id, "status": "已打包待揽收", "carrier": "顺丰", "updated": "今天 09:20"},
        ensure_ascii=False,
    )


TOOL_IMPL = {"get_weather": get_weather, "get_order_status": get_order_status}


def main() -> int:
    configure_stdout()

    strict_choice = "--break" in sys.argv
    if strict_choice:
        print("=" * 62)
        print('实验：thinking 模式 + tool_choice="required"')
        print("=" * 62)

    # max_retries=0：要看清"第一次失败"的原始报错，别让 SDK 悄悄重试
    client = get_client(timeout=120.0, max_retries=0)

    # 模型不知道今天是几号，日期必须由你提供，否则它没法把"明天"换成具体日期
    today = datetime.date.today().isoformat()
    messages = [
        {
            "role": "system",
            "content": f"你是一个助手。需要实时信息时先调用工具，不要靠猜。今天是 {today}。",
        },
        {
            "role": "user",
            "content": "杭州明天天气怎么样？再帮我查一下订单 A20261015001 到哪了。",
        },
    ]

    for round_no in range(1, 6):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                # thinking 模式下只能用 auto，不能用 required 或指定函数
                tool_choice="required" if strict_choice else "auto",
                extra_body={"thinking": {"type": "enabled"}},
            )
        except APIStatusError as exc:
            print(f"\n[错误] {exc.status_code}：{exc.message}")
            if exc.status_code == 400 and strict_choice:
                print("\n服务端说得很清楚：thinking 模式不支持这个 tool_choice。")
                print("两种修法：")
                print("  1. 改回 tool_choice=\"auto\"（推荐，让模型自己决定要不要调）")
                print('  2. 确实需要强制调用，就用 extra_body={"thinking": {"type": "disabled"}} 关掉思考')
                return 0
            print(f"\n[错误] {explain_error(exc)}")
            return 1
        except Exception as exc:
            print(f"\n[错误] {explain_error(exc)}")
            return 1

        message = response.choices[0].message
        reasoning = getattr(message, "reasoning_content", "") or ""

        # assistant 这一轮要完整回存：思考内容 + tool_calls 都得带上
        entry: dict = {"role": "assistant", "content": message.content}
        if message.tool_calls:
            entry["tool_calls"] = [call.model_dump() for call in message.tool_calls]
        if reasoning:
            entry["reasoning_content"] = reasoning
        messages.append(entry)

        if not message.tool_calls:
            print(f"\n[第 {round_no} 轮] 最终回答：")
            print(message.content)
            usage = response.usage
            if usage:
                print(f"\n用量：输入 {usage.prompt_tokens} / 输出 {usage.completion_tokens} tokens")
            return 0

        print(f"\n[第 {round_no} 轮] 模型申请调用 {len(message.tool_calls)} 个工具")
        print(f"          （本轮思考 {len(reasoning)} 字，已随消息回存）")

        for call in message.tool_calls:
            name = call.function.name
            raw_args = call.function.arguments
            print(f"  模型想调用：{name}({raw_args})")

            handler = TOOL_IMPL.get(name)
            if handler is None:
                result = json.dumps({"error": f"没有这个工具：{name}"}, ensure_ascii=False)
            else:
                try:
                    # 参数是模型生成的 JSON 字符串，必须自己解析、自己校验
                    result = handler(**json.loads(raw_args))
                except Exception as exc:
                    # 工具报错也要作为结果回给模型，让它自己决定怎么弥补
                    result = json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)

            print(f"  本地执行结果：{result}")

            # 结果用 role=tool 回传，tool_call_id 必须和请求对上，否则模型会乱
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    print("\n[放弃] 超过 5 轮还在调工具，可能陷入循环，停手。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
