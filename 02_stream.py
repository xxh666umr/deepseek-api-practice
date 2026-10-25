"""02 流式输出：回答边生成边打印，像聊天窗口那样一个字一个字冒出来。

用法：
    python 02_stream.py                              # 默认问题，关闭思考模式
    python 02_stream.py "讲个冷笑话"                  # 换问题
    python 02_stream.py --think "9.11 和 9.8 哪个大"   # 打开思考模式，顺便看思考过程

和 01 的区别只有一个：请求里加了 stream=True。
返回的不再是一个完整对象，而是一个"迭代器"，每转一圈吐一小段文字（delta）。
"""

from __future__ import annotations

import sys
import time

from common import MODEL, configure_stdout, explain_error, get_client

DEFAULT_PROMPT = "用大约 200 字讲清楚什么是流式输出，以及它为什么让聊天界面感觉更快。"


def main() -> int:
    configure_stdout()

    argv = sys.argv[1:]
    thinking = "--think" in argv
    words = [a for a in argv if a != "--think"]
    prompt = " ".join(words) if words else DEFAULT_PROMPT

    client = get_client(timeout=120.0)

    messages = [
        {"role": "system", "content": "你是一个说话简洁的中文助手。"},
        {"role": "user", "content": prompt},
    ]

    print(f"-> 模型 {MODEL}   流式    thinking={thinking}")
    print(f"   问题：{prompt}\n")
    print("-" * 60)

    started = time.perf_counter()
    first_token_at: float | None = None
    answer_started = False
    reasoning_chars = 0
    answer_chars = 0
    usage = None
    finish_reason = None

    try:
        stream = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            stream=True,
            # 不加这句，流式拿不到 usage：最后一个 chunk 才会带上完整统计
            stream_options={"include_usage": True},
            # thinking 不是 OpenAI 标准字段，得塞进 extra_body；
            # 不传的话默认是开启的，回答前会先消耗一段输出 token 做思考。
            extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}},
        )

        for chunk in stream:
            # 最后一个 chunk 带用量，通常没有 choices，所以先取 usage 再判空
            if chunk.usage:
                usage = chunk.usage
            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason

            delta = choice.delta

            # 思考模式下，模型先吐 reasoning_content（思考），再吐 content（正文）
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                if first_token_at is None:
                    first_token_at = time.perf_counter() - started
                reasoning_chars += len(reasoning)
                if thinking:
                    print(reasoning, end="", flush=True)

            if delta.content:
                if first_token_at is None:
                    first_token_at = time.perf_counter() - started
                if not answer_started:
                    answer_started = True
                    if thinking and reasoning_chars:
                        print("\n" + "-" * 60 + "\n[正式回答]")
                answer_chars += len(delta.content)
                print(delta.content, end="", flush=True)

    except KeyboardInterrupt:
        print("\n[中断] 你按了 Ctrl+C。流式请求一旦断开，已生成的部分不会退费。")
        return 130
    except Exception as exc:  # 网络、鉴权、状态码错误统一翻译
        print()
        print(f"[错误] {explain_error(exc)}")
        return 1

    elapsed = time.perf_counter() - started
    print("\n" + "-" * 60)

    if finish_reason == "length":
        print("[注意] 撞到 max_tokens 上限，回答被截断了。")

    if usage:
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        cache_hit = getattr(prompt_details, "prompt_cache_hit_tokens", None)
        completion_details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(completion_details, "reasoning_tokens", None)

        cache = f"，缓存命中 {cache_hit}" if cache_hit else ""
        think = f"，其中思考 {reasoning_tokens}" if reasoning_tokens else ""
        print(
            f"[用量] 输入 {usage.prompt_tokens} tokens{cache}；"
            f"输出 {usage.completion_tokens} tokens{think}"
        )

    if first_token_at is not None:
        speed = answer_chars / elapsed if elapsed > 0 else 0.0
        print(
            f"[耗时] 首字 {first_token_at:.2f}s，全程 {elapsed:.2f}s，"
            f"正文 {answer_chars} 字，约 {speed:.1f} 字/秒"
        )
    else:
        print("[提示] 一个字符都没收到，检查一下网络或请求参数。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
