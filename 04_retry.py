"""04 超时与重试：让程序在网络抖动时活下来，而不是直接崩。

两个参数决定"能跑"和"能用"的差距：
    timeout      等多久算失败。SDK 默认 10 分钟，对聊天界面来说太长了。
    max_retries  失败后自动重试几次。SDK 默认 2 次。

用法：
    python 04_retry.py                      # 模拟故障，看每次重试（不发真实请求，不花钱）
    python 04_retry.py --real               # 对真实接口跑一次，确认封装没搞坏正常路径
    python 04_retry.py --real --timeout 20  # 指定超时秒数

核心结论先给你：
    能重试的：连接失败、超时、429 限流、5xx 服务端错误
    不能重试的：401 鉴权失败、400 参数错误、404 模型不存在
    对不能重试的错误反复重试，只是在浪费时间和钱。
"""

from __future__ import annotations

import random
import sys
import time

import httpx2
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from common import MODEL, configure_stdout, explain_error, get_client

# 这些错误无论重试多少次结果都一样，立刻失败才是对的
NON_RETRYABLE = (
    AuthenticationError,     # 401
    PermissionDeniedError,   # 403
    BadRequestError,         # 400
    NotFoundError,           # 404
)

# 这些是"可能自己好起来"的
RETRYABLE = (
    APITimeoutError,       # 请求超时
    APIConnectionError,    # 连不上、被重置
    RateLimitError,        # 429
    InternalServerError,   # 5xx
)


def retry_delay(exc: Exception, attempt: int, base: float, cap: float) -> tuple[float, str]:
    """算这次该等多久，并说明理由。

    优先听服务端的 Retry-After（它比你更清楚多久能缓过来）；
    否则指数退避：0.5s、1s、2s、4s...；
    最后加一点随机抖动，避免多个客户端同一秒一起重试把服务端再打垮。
    """
    response = getattr(exc, "response", None)
    if response is not None:
        header = response.headers.get("retry-after")
        if header:
            try:
                return min(float(header), cap), f"服务端要求等 {header}s"
            except ValueError:
                pass

    delay = min(cap, base * (2 ** (attempt - 1))) * random.uniform(0.8, 1.2)
    return delay, f"指数退避，基准 {base}s 起翻倍 + 抖动"


def create_with_retry(
    create,
    *,
    max_attempts: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    log=print,
):
    """把一次"发请求"的动作包起来，失败按规则重试。

    create 是一个无参函数，返回结果。用函数而不是 client 传进来，
    是为了让模拟故障和真实请求能共用同一套重试逻辑。
    """
    for attempt in range(1, max_attempts + 1):
        started = time.perf_counter()
        try:
            result = create()
            log(f"[尝试 {attempt}] 成功，用时 {time.perf_counter() - started:.2f}s")
            return result

        except NON_RETRYABLE as exc:
            # 这里必须分清：401 重试 4 次 = 浪费 4 倍的失败时间，还可能触发风控
            log(f"[尝试 {attempt}] {type(exc).__name__}：这类错误重试没用，立刻失败。")
            raise

        except RETRYABLE as exc:
            if attempt == max_attempts:
                log(f"[尝试 {attempt}] {type(exc).__name__}：{max_attempts} 次都失败，放弃。")
                raise
            delay, reason = retry_delay(exc, attempt, base_delay, max_delay)
            log(f"[尝试 {attempt}] {type(exc).__name__}，{delay:.1f}s 后重试（{reason}）")
            time.sleep(delay)

    raise RuntimeError("不会走到这里")


# ---------------------------------------------------------------------------
# 下面是"造故障"的部分，只为了让重试过程看得见
# ---------------------------------------------------------------------------

def fake_response(status: int, retry_after: str | None = None) -> httpx2.Response:
    request = httpx2.Request("POST", "https://api.deepseek.com/chat/completions")
    headers = {"retry-after": retry_after} if retry_after else {}
    return httpx2.Response(status, headers=headers, request=request, json={"error": {"message": "fake"}})


def make_exception(kind: str) -> Exception:
    if kind == "conn":
        return APIConnectionError(request=None)
    if kind == "timeout":
        return APITimeoutError(request=None)
    if kind == "429":
        # 服务端明确说"0.3 秒后再来"，比任何指数退避都权威
        return RateLimitError("Rate limit exceeded", response=fake_response(429, "0.3"), body=None)
    if kind == "500":
        return InternalServerError("Internal server error", response=fake_response(500), body=None)
    if kind == "401":
        return AuthenticationError("Invalid API key", response=fake_response(401), body=None)
    raise ValueError(kind)


class FakeResult:
    """装作是接口返回的对象，只实现用到的那几个属性。"""

    class _Message:
        content = "（这是模拟出来的回答，没有真的发请求）"

    class _Choice:
        message = None

    class _Choices(list):
        pass

    def __init__(self):
        choice = FakeResult._Choice()
        choice.message = FakeResult._Message()
        self.choices = [choice]


def fake_create(failures: list[str]):
    """前几次抛指定异常，之后返回假结果。"""
    remaining = list(failures)

    def create():
        if remaining:
            raise make_exception(remaining.pop(0))
        return FakeResult()

    return create


def simulate() -> int:
    print("=" * 62)
    print("场景 1：连接失败 → 超时 → 429（要求等 0.3 秒）→ 500 → 成功")
    print("=" * 62)
    started = time.perf_counter()
    result = create_with_retry(fake_create(["conn", "timeout", "429", "500"]), max_attempts=5)
    print(f"结果：{result.choices[0].message.content}")
    print(f"总耗时 {time.perf_counter() - started:.1f}s（大部分是等待）\n")

    print("=" * 62)
    print("场景 2：401 鉴权失败（Key 写错了）")
    print("=" * 62)
    try:
        create_with_retry(fake_create(["401"]))
    except AuthenticationError:
        print("直接抛给上层了，没有浪费 4 次尝试。\n")

    print("=" * 62)
    print("要点")
    print("=" * 62)
    print("1. 每次重试的等待时间都能看到理由：前几次是指数退避，429 那次是听服务端的")
    print("2. 注意 429 只等了 0.3s，比死板的 1s/2s/4s 更聪明，也不容易把服务端打死")
    print("3. 401 一次就停：重试不改变结果，只会让用户多等、让你多花钱")
    print("4. 真实项目还要限制总时长，别让重试把一次请求拖成几分钟")
    return 0


def run_real(timeout: float) -> int:
    # 关键：max_retries=0，把自己写的重试和 SDK 自带的重试分开，
    # 否则 SDK 重试 2 次 × 自己重试 4 次 = 最坏 8 次请求。
    client = get_client(timeout=timeout, max_retries=0)
    print(f"真实请求 | 超时 {timeout}s | SDK 自动重试已关闭，由本脚本控制\n")

    def create():
        return client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "只回复两个字：收到"}],
            extra_body={"thinking": {"type": "disabled"}},
        )

    try:
        response = create_with_retry(create, max_attempts=3)
    except Exception as exc:
        print(f"\n[错误] {explain_error(exc)}")
        return 1

    print(f"\n回答：{response.choices[0].message.content}")
    usage = getattr(response, "usage", None)
    if usage:
        print(f"用量：输入 {usage.prompt_tokens} / 输出 {usage.completion_tokens} tokens")
    return 0


def main() -> int:
    configure_stdout()
    args = sys.argv[1:]

    if "--real" not in args:
        return simulate()

    timeout = 30.0
    if "--timeout" in args:
        index = args.index("--timeout")
        if index + 1 < len(args):
            try:
                timeout = float(args[index + 1])
            except ValueError:
                print("--timeout 后面要跟数字，比如 --timeout 20")
                return 2

    return run_real(timeout)


if __name__ == "__main__":
    sys.exit(main())
