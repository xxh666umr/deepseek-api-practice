"""05 稳定输出 JSON：让模型返回程序能吃的数据。

用法：
    python 05_json.py

两条必须记住的规则：

1. 光设 response_format={"type": "json_object"} 不够。
   官方文档明确警告：如果提示词里没要求输出 JSON，模型可能一直生成空白字符，
   直到撞上 token 上限，表现为"请求卡住了"。所以提示词必须明确要求 JSON。

2. json_object 只保证"能解析"，不保证"字段对"。
   你想要的字段名、类型、枚举值，模型都可能给你换成别的。
   所以解析之后必须校验，校验不过就把错误回给模型让它修一次。
"""

from __future__ import annotations

import json

from common import MODEL, configure_stdout, explain_error, get_client

# 一段很日常的客服留言：信息散落在句子里，还有多余格式
TICKET = (
    "张伟，电话 138-0000-1234，邮箱 zhangwei@example.com，上周三在你们官网下单买了台 27 寸显示器，"
    "订单号 A20261015001，到现在还没发货。他很生气，说再不发货就退款，要求今天之内给答复。"
)

SYSTEM = "你负责把用户消息抽取成结构化数据。你只输出 JSON，不输出解释，不加代码块标记。"

SCHEMA_HINT = """{
  "name": 字符串或 null,
  "phone": 字符串或 null（只要数字，去掉横线和空格）,
  "email": 字符串或 null,
  "order_id": 字符串或 null,
  "product": 字符串或 null,
  "issue": 字符串或 null（一句话概括问题）,
  "urgency": "low" 或 "medium" 或 "high",
  "refund_requested": true 或 false
}"""

USER = f"""从下面这段客服留言里抽取信息，严格按这个结构输出 JSON：

{SCHEMA_HINT}

规则：
1. 只输出这一个 JSON 对象。不要 markdown 代码块，不要任何说明文字。
2. 原文没有的信息填 null，不要自己编。
3. urgency 只能从 low / medium / high 里选一个。
4. refund_requested 必须是布尔值 true 或 false，不能是字符串。

原文：
{TICKET}
"""

REQUIRED_FIELDS = (
    "name",
    "phone",
    "email",
    "order_id",
    "product",
    "issue",
    "urgency",
    "refund_requested",
)

STRING_FIELDS = ("name", "phone", "email", "order_id", "product", "issue")


def validate(data) -> list[str]:
    """校验结构，而不只是"能解析"。返回问题清单，空列表表示合格。"""
    if not isinstance(data, dict):
        return [f"顶层应该是一个 JSON 对象，实际是 {type(data).__name__}"]

    problems: list[str] = []

    for key in REQUIRED_FIELDS:
        if key not in data:
            problems.append(f"缺少字段 {key}")

    for key in STRING_FIELDS:
        value = data.get(key)
        if key in data and not isinstance(value, (str, type(None))):
            problems.append(f"{key} 应该是字符串或 null，实际是 {type(value).__name__}")

    urgency = data.get("urgency")
    if urgency not in ("low", "medium", "high"):
        problems.append(f"urgency 必须是 low/medium/high，实际是 {urgency!r}")

    if not isinstance(data.get("refund_requested"), bool):
        problems.append(
            "refund_requested 必须是布尔值 true/false，"
            f"实际是 {type(data.get('refund_requested')).__name__}"
        )

    phone = data.get("phone")
    if isinstance(phone, str) and any(ch in phone for ch in "- "):
        problems.append("phone 里还留着横线或空格，应该只保留数字")

    return problems


def parse_and_check(raw: str | None):
    """把原始文本变成 (数据, 问题清单)。"""
    if not raw:
        return None, ["模型返回了空内容"]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"不是合法 JSON：{exc.msg}（第 {exc.pos} 个字符处）"]
    return data, validate(data)


def request_json(client, messages, max_tokens: int = 600):
    """发一次请求，强制 JSON 输出。

    max_tokens 是保险丝：万一模型开始输出空白（文档里警告的那种情况），
    上限能把它掐断，而不是让你干等几分钟。
    """
    return client.chat.completions.create(
        model=MODEL,
        messages=messages,
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        extra_body={"thinking": {"type": "disabled"}},
    )


def main() -> int:
    configure_stdout()
    client = get_client(timeout=60.0, max_retries=1)

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER},
    ]

    print("-> 发送请求，response_format=json_object\n")

    try:
        response = request_json(client, messages)
    except Exception as exc:
        print(f"[错误] {explain_error(exc)}")
        return 1

    raw = response.choices[0].message.content
    finish_reason = response.choices[0].finish_reason

    print("--- 模型原始输出 ---")
    print(raw)
    print("--------------------\n")

    if finish_reason == "length":
        print("[注意] finish_reason=length：撞到 max_tokens 上限，JSON 一定是残缺的。")
        print("       这种时候重试同样的请求没用，要么调大上限，要么把任务拆小。\n")

    data, problems = parse_and_check(raw)

    # 第一次不合格就带着具体问题请模型修一次，这比直接报错给用户友好得多
    if problems:
        print("校验没通过：")
        for problem in problems:
            print(f"  - {problem}")

        if finish_reason == "length":
            print("\n因为是被截断，修复也没意义，直接失败。")
            return 1

        print("\n把问题回给模型，让它修一次...\n")
        messages.append({"role": "assistant", "content": raw})
        messages.append(
            {
                "role": "user",
                "content": "你上面的输出有这些问题：\n"
                + "\n".join(f"- {p}" for p in problems)
                + "\n请只输出修正后的 JSON 对象，不要解释。",
            }
        )

        try:
            response = request_json(client, messages)
        except Exception as exc:
            print(f"[错误] {explain_error(exc)}")
            return 1

        raw = response.choices[0].message.content
        print("--- 修复后的输出 ---")
        print(raw)
        print("--------------------\n")
        data, problems = parse_and_check(raw)

    if problems:
        print("修复后仍然不合格，正确做法是降级处理（转人工 / 用旧值 / 报错给用户），而不是硬塞。")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("校验通过，解析结果：\n")
    for key in REQUIRED_FIELDS:
        print(f"  {key:18s} {data.get(key)!r}")

    usage = response.usage
    if usage:
        print(f"\n用量：输入 {usage.prompt_tokens} / 输出 {usage.completion_tokens} tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
