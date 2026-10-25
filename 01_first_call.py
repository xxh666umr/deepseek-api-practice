"""第一个 API 调用：把一句话发给 DeepSeek，把回答打印出来。

用法（在本文件所在目录打开终端）：
    python 01_first_call.py

这个脚本只做三件事：
1. 从 .env 里读 API Key；
2. 用 openai 这个 SDK 连 DeepSeek 的接口，发一条消息；
3. 打印回答和 token 用量，并把常见错误翻译成人话。
"""

import os
import sys
from pathlib import Path

# Windows 控制台默认可能是 GBK，中文容易变成乱码。
# 这里强制用 UTF-8 输出（VS Code / Windows Terminal 都支持）。
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    OpenAI,
)

# ---------------------------------------------------------------------------
# 配置区：这三行是你要理解的核心
# ---------------------------------------------------------------------------

# DeepSeek 兼容 OpenAI 协议，所以只要把 base_url 指过来，openai 这个 SDK 就能用。
# 默认值是 https://api.openai.com/v1，指向别处就会报 404 或 401。
BASE_URL = "https://api.deepseek.com"

# 模型名必须和平台当前提供的一致。deepseek-chat 这类旧名字已经不在文档里了。
# 可选：deepseek-flash（便宜、快）、deepseek-v4-pro（更强、贵）
MODEL = "deepseek-flash"

# Key 不写在代码里，从 .env 读，避免把密钥提交进 git。
ENV_FILE = Path(__file__).with_name(".env")


def load_env(path: Path = ENV_FILE) -> None:
    """把 .env 里的 KEY=VALUE 读进 os.environ（已存在的环境变量优先，不覆盖）。

    正式项目里用 python-dotenv 一行就够：
        from dotenv import load_dotenv; load_dotenv()
    这里手写 8 行，只是为了让第一个练习不必多装一个包。
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # 允许写成 DEEPSEEK_API_KEY="sk-xxx" 这种带引号的形式
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    load_env()

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("没有找到 DEEPSEEK_API_KEY。")
        print(f"请打开 {ENV_FILE}，把 Key 填在 DEEPSEEK_API_KEY= 后面，例如：")
        print("    DEEPSEEK_API_KEY=sk-1234567890abcdef")
        print("Key 在 https://platform.deepseek.com/api_keys 申请。")
        return 1

    # client 只负责"怎么连"，不负责"发什么"。
    # timeout=60 表示单次请求最多等 60 秒，超时会抛 APIConnectionError。
    client = OpenAI(api_key=api_key, base_url=BASE_URL, timeout=60.0)

    # messages 是一个列表，每条消息有 role 和 content。
    # system 定人设，user 是你说的话；后面多轮对话就是往这个列表里继续 append。
    messages = [
        {"role": "system", "content": "你是一个说话简洁的中文助手。"},
        {"role": "user", "content": "用三句话介绍你自己，并说明你是哪个模型。"},
    ]

    print(f"-> 请求 {BASE_URL} ，模型 {MODEL} ，请稍等...")

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            # 可选参数，先认识不做：temperature=0.7 控制随机性，max_tokens 限制长度
        )
    except AuthenticationError:
        print("[错误] 401 鉴权失败：Key 不对、已被删除，或者粘贴时带了多余空格/引号。")
        return 1
    except APIConnectionError as exc:
        print(f"[错误] 网络没连上 {BASE_URL} ：{exc}")
        print("       如果开了代理，确认命令行也走代理；访问 DeepSeek 一般直连即可。")
        return 1
    except APIStatusError as exc:
        print(f"[错误] 接口返回 {exc.status_code}：{exc.message}")
        if exc.status_code == 402:
            print("       账户余额不足，去 https://platform.deepseek.com 充值。")
        elif exc.status_code == 404:
            print(f"       模型名可能不对，当前可用：deepseek-flash / deepseek-v4-pro。")
        elif exc.status_code == 429:
            print("       请求太频繁，等几秒再试。")
        elif exc.status_code >= 500:
            print("       服务端问题，稍后重试。")
        return 1

    # 返回的是一个对象，正文在 choices[0].message.content
    print("\n--- 回答 ---")
    print(resp.choices[0].message.content)

    # 用量是按 token 计费的依据，练习阶段养成看一眼的习惯
    usage = resp.usage
    if usage:
        print("\n--- 用量 ---")
        print(
            f"输入 {usage.prompt_tokens} tokens，"
            f"输出 {usage.completion_tokens} tokens，"
            f"合计 {usage.total_tokens} tokens"
        )
        # 不是所有服务都会回 request id，取不到就不打印，别显示 None
        req_id = getattr(resp, "_request_id", None)
        if req_id:
            print(f"本次请求 id：{req_id}（排查问题时有用）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
