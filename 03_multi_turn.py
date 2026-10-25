"""03 多轮对话：让模型"记住"上文。

先说真相：模型不记事。每次请求都是无状态的，它看到的只有你这次发过去的 messages。
所谓"它记住了我叫小林"，是因为你把前面几轮对话一起重新发了过去。

所以在代码里，多轮对话的全部秘密就是：把历史攒起来，每次整份发出去。

用法：
    python 03_multi_turn.py            # 交互模式，输入 /help 看命令
    python 03_multi_turn.py --demo     # 跑固定剧本，并和"没有上下文"做对照
"""

from __future__ import annotations

import sys

from common import (
    MODEL,
    configure_stdout,
    explain_error,
    get_client,
    sanitize,
    stream_chat,
)

SYSTEM_PROMPT = "你是一个说话简洁的中文助手。记住对话里出现过的信息，被问到时准确回答。"

DEMO_TURNS = [
    "我叫小林，我的猫叫团子。请记住这两个名字。",
    "我的猫叫什么名字？",
    "我叫什么名字？",
]

HELP = """
命令：
  /help     显示这份帮助
  /history  查看当前上下文里有哪些消息
  /think    开关思考模式（默认关）
  /clear    清空上下文，从头开始
  /exit     退出
直接输入文字就是提问。
""".strip()


class Session:
    """一次会话 = 一份 messages 列表 + 一些统计。"""

    def __init__(self, client, thinking: bool = False):
        self.client = client
        self.thinking = thinking
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.turn = 0
        self.total_in = 0
        self.total_out = 0
        self.total_reasoning = 0

    def ask(self, prompt: str) -> dict:
        self.turn += 1

        # 关键的一行：先把这一问追加进历史，然后把"整个历史"发给服务端
        self.messages.append({"role": "user", "content": prompt})

        print(f"[第 {self.turn} 轮 | 本次发送 {len(self.messages)} 条消息]")

        result = stream_chat(self.client, self.messages, thinking=self.thinking)

        # 只回存 role 和 content。
        # reasoning_content（思考过程）在"不带 tools"的请求里不需要回传，
        # 传了服务端也会忽略；但一旦加了 tools，就必须原样带回去，否则报 400。
        self.messages.append({"role": "assistant", "content": result["text"]})

        usage = result["usage"]
        if usage:
            self.total_in += usage.prompt_tokens
            self.total_out += usage.completion_tokens
            details = getattr(usage, "completion_tokens_details", None)
            self.total_reasoning += getattr(details, "reasoning_tokens", 0) or 0
            print(
                f"\n[本轮] 输入 {usage.prompt_tokens} / 输出 {usage.completion_tokens} tokens"
                f"    [累计] 输入 {self.total_in} / 输出 {self.total_out} tokens"
            )

        return result

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.turn = 0
        print("上下文已清空，模型现在对你的过去一无所知。")

    def show_history(self) -> None:
        for index, message in enumerate(self.messages):
            body = message["content"].replace("\n", " ")
            if len(body) > 60:
                body = body[:60] + "..."
            print(f"  {index:02d}  {message['role']:9s}  {body}")
        print(f"  合计 {len(self.messages)} 条消息")


def run_demo(session: Session) -> None:
    print(f"模型 {MODEL}，先跑 A 组：带着完整上下文连问三轮。")

    for prompt in DEMO_TURNS:
        print(f"\n你：{prompt}")
        session.ask(prompt)

    print("\n" + "=" * 60)
    print("B 组：换成一份全新上下文，只问同一个问题")

    fresh = Session(session.client, thinking=session.thinking)
    print(f"\n你：{DEMO_TURNS[2]}")
    fresh.ask(DEMO_TURNS[2])

    print("\n" + "=" * 60)
    print("对照结果：A 组答得出名字，B 组答不出。")
    print("模型本身不记事，所谓记忆，就是你每次发给它的 messages。")


def main() -> int:
    configure_stdout()
    client = get_client(timeout=120.0)
    session = Session(client)

    if "--demo" in sys.argv:
        run_demo(session)
        return 0

    print(HELP)
    while True:
        try:
            line = sanitize(input("\n你：")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            break

        if not line:
            continue
        if line in ("/exit", "/quit", "exit", "quit"):
            print("已退出。")
            break
        if line == "/help":
            print(HELP)
            continue
        if line == "/history":
            session.show_history()
            continue
        if line == "/think":
            session.thinking = not session.thinking
            print(f"思考模式：{'开' if session.thinking else '关'}")
            continue
        if line == "/clear":
            session.reset()
            continue

        try:
            session.ask(line)
        except Exception as exc:
            print(f"\n[错误] {explain_error(exc)}")
            # 出错时把刚追加的那条 user 消息撤回，别让脏数据留在历史里
            if session.messages and session.messages[-1]["role"] == "user":
                session.messages.pop()
            session.turn -= 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
