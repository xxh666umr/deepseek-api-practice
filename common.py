"""几个练习脚本共用的部分：读 Key、建 client、把报错翻译成中文。

01_first_call.py 里这些是摊开写的，方便你一眼看全；
从 02 开始抽到这里复用，避免每个脚本都抄一遍。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from openai import APIConnectionError, APIStatusError, AuthenticationError, OpenAI

BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-flash"      # 便宜快
MODEL_PRO = "deepseek-v4-pro"  # 更强，贵一倍多

ENV_FILE = Path(__file__).with_name(".env")


def configure_stdout() -> None:
    """Windows 控制台默认可能是 GBK，强制 UTF-8 输出，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def load_env(path: Path = ENV_FILE) -> None:
    """把 .env 里的 KEY=VALUE 读进 os.environ（已存在的环境变量优先）。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require_key() -> str:
    load_env()
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        print(f"没有找到 DEEPSEEK_API_KEY，请先在 {ENV_FILE} 里填好。")
        raise SystemExit(1)
    return key


def get_client(timeout: float = 60.0, max_retries: int = 2) -> OpenAI:
    """max_retries=2 是 SDK 默认值：遇到网络错误/429/5xx 会自动重试两次。

    如果你自己写重试循环，记得传 max_retries=0，否则两层重试会相乘。
    """
    return OpenAI(
        api_key=require_key(),
        base_url=BASE_URL,
        timeout=timeout,
        max_retries=max_retries,
    )


def sanitize(text: str) -> str:
    """清洗无法编码的字符（例如管道输入被错误解码产生的代理对）。

    这类字符留在字符串里，等到发请求时才炸，报错信息还很难懂：
    UnicodeEncodeError: 'utf-8' codec can't encode character '\\udcab' ...
    在入口处替换掉，比在深层报错时排查划算得多。
    """
    return text.encode("utf-8", "replace").decode("utf-8")


def stream_chat(
    client: OpenAI,
    messages: list[dict],
    *,
    thinking: bool = False,
    model: str = MODEL,
    show_thinking: bool = False,
    out=None,
):
    """流式发一次请求，边打印边收集，返回结果字典。

    这就是 02_stream.py 里那段 for 循环的封装版：02 用来看清机制，
    03 之后直接调用，不必每个脚本重写一遍。

    返回 {
        "text": 正文全文, "reasoning": 思考全文, "usage": 用量对象,
        "first_token_at": 首字耗时(秒), "elapsed": 总耗时(秒), "finish_reason": 结束原因
    }
    """
    out = out or sys.stdout
    started = time.perf_counter()
    first_token_at: float | None = None
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage = None
    finish_reason = None

    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
        extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}},
    )

    for chunk in stream:
        if chunk.usage:
            usage = chunk.usage
        if not chunk.choices:
            continue

        choice = chunk.choices[0]
        if choice.finish_reason:
            finish_reason = choice.finish_reason

        delta = choice.delta

        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            if first_token_at is None:
                first_token_at = time.perf_counter() - started
            reasoning_parts.append(reasoning)
            if show_thinking:
                out.write(reasoning)
                out.flush()

        if delta.content:
            if first_token_at is None:
                first_token_at = time.perf_counter() - started
            text_parts.append(delta.content)
            out.write(delta.content)
            out.flush()

    return {
        "text": "".join(text_parts),
        "reasoning": "".join(reasoning_parts),
        "usage": usage,
        "first_token_at": first_token_at,
        "elapsed": time.perf_counter() - started,
        "finish_reason": finish_reason,
    }


def explain_error(exc: Exception) -> str:
    """把 SDK 抛出的异常翻译成能照着处理的中文。"""
    if isinstance(exc, AuthenticationError):
        return "401 鉴权失败：Key 不对、已被删除，或者粘贴时多了空格/引号。"
    if isinstance(exc, APIConnectionError):
        return (
            f"网络没连上 {BASE_URL}：{exc}\n"
            "       如果开了代理，确认命令行也走代理；访问 DeepSeek 一般直连即可。"
        )
    if isinstance(exc, APIStatusError):
        advice = {
            400: "参数有问题，先看上面的 message。",
            402: "账户余额不足，去 https://platform.deepseek.com 充值。",
            404: "模型名可能不对，当前可用：deepseek-flash / deepseek-v4-pro。",
            429: "请求太频繁，等几秒再试。",
            500: "服务端问题，稍后重试。",
        }.get(exc.status_code)
        if not advice and exc.status_code >= 500:
            advice = "服务端问题，稍后重试。"
        text = f"接口返回 {exc.status_code}：{exc.message}"
        return text + (f"\n       {advice}" if advice else "")
    return f"{type(exc).__name__}: {exc}"
