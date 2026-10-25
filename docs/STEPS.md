# 逐步笔记：八个模块的设计与实测

> 这是仓库早期“八步练习”的完整笔记，保留了每一步的实测输出、踩过的坑和报错对照。
> 想先跑起来，看 [README](../README.md)；想理解某个模块为什么这么写，看这里。

---

## 第 1 步：第一次调用

```powershell
python 01_first_call.py
```

三个核心参数：`base_url`（请求发给谁）、`api_key`（扣谁的钱）、`model`（用哪个模型）。
回答在 `resp.choices[0].message.content`，用量在 `resp.usage`。

## 第 2 步：流式输出

```powershell
python 02_stream.py                              # 默认问题，关闭思考
python 02_stream.py "讲个冷笑话"                  # 换问题
python 02_stream.py --think "9.11 和 9.8 哪个大"   # 打开思考模式
```

和 01 的差别只有一个 `stream=True`：返回值从"一个完整对象"变成"一个迭代器"，每次循环吐一小段 `delta`。

实测（thinking 关闭）：

```
[用量] 输入 35 tokens；输出 122 tokens
[耗时] 首字 0.70s，全程 1.80s，正文 217 字，约 120.8 字/秒
```

实测（`--think`，问 9.11 和 9.8）：输入 55 / 输出 55，**其中思考 50**。答案只有 5 个 token，思考花了 50 个，同价计费。

三个必须知道的细节：

| 细节 | 说明 |
|---|---|
| `stream_options={"include_usage": True}` | 不加这句，流式响应里**拿不到 usage**。用量挂在最后一个 chunk 上，而那个 chunk 的 `choices` 是空的 |
| `extra_body={"thinking": ...}` | `thinking` 是 DeepSeek 扩展字段，SDK 不认，只能塞进 `extra_body`。**不传默认开启** |
| 思考 vs 正文 | 思考走 `delta.reasoning_content`，正文走 `delta.content` |

**为什么要流式**：总耗时不变，变的是首字时间 1.80s → 0.70s。用户感觉"快"来自立刻有反馈。

## 第 3 步：多轮对话

```powershell
python 03_multi_turn.py            # 交互模式，可 /help /history /think /clear /exit
python 03_multi_turn.py --demo     # 固定剧本 + 无上下文对照
```

实测 `--demo`：

```
你：我叫小林，我的猫叫团子。请记住这两个名字。
[第 1 轮 | 本次发送 2 条消息]
好的，小林，我记住了：你叫小林，你的猫叫团子。
[本轮] 输入 40 / 输出 17 tokens    [累计] 输入 40 / 输出 17 tokens

你：我的猫叫什么名字？
[第 2 轮 | 本次发送 4 条消息]
你的猫叫团子。
[本轮] 输入 66 / 输出 6 tokens    [累计] 输入 106 / 输出 23 tokens

你：我叫什么名字？
[第 3 轮 | 本次发送 6 条消息]
你叫小林。
[本轮] 输入 80 / 输出 4 tokens    [累计] 输入 186 / 输出 27 tokens

B 组：换成一份全新上下文，只问同一句
你：我叫什么名字？
你还没有告诉我你的名字。
```

**模型不记事。** 每次请求都是无状态的，它看到的只有这次发过去的 `messages`。
所谓记忆 = 把历史攒起来，每次整份重发。所以输入 token 40 → 66 → 80 一路涨，对话越长越贵。

两个坑：

1. **`reasoning_content` 回不回传取决于有没有 `tools`**：不带 `tools` 时不用回传（传了也忽略）；一旦带 `tools`，所有历史的思考内容都必须原样带回，否则 **400**。
2. **Windows 管道传中文要加 `-X utf8`**，否则解出非法代理对，发送时报 `UnicodeEncodeError ... '\udcab'`。

## 第 4 步：超时与重试

```powershell
python 04_retry.py                      # 模拟故障，不发真实请求、不花钱
python 04_retry.py --real               # 真实请求，确认封装没搞坏正常路径
python 04_retry.py --real --timeout 20  # 指定超时秒数
```

模拟输出（真跑的结果）：

```
场景 1：连接失败 → 超时 → 429（要求等 0.3 秒）→ 500 → 成功
[尝试 1] APIConnectionError，0.4s 后重试（指数退避，基准 0.5s 起翻倍 + 抖动）
[尝试 2] APITimeoutError，1.1s 后重试（指数退避，基准 0.5s 起翻倍 + 抖动）
[尝试 3] RateLimitError，0.3s 后重试（服务端要求等 0.3s）
[尝试 4] InternalServerError，4.6s 后重试（指数退避，基准 0.5s 起翻倍 + 抖动）
[尝试 5] 成功，用时 0.00s

场景 2：401 鉴权失败（Key 写错了）
[尝试 1] AuthenticationError：这类错误重试没用，立刻失败。
直接抛给上层了，没有浪费 4 次尝试。
```

真实路径：

```
真实请求 | 超时 30.0s | SDK 自动重试已关闭，由本脚本控制
[尝试 1] 成功，用时 0.69s
回答：收到
用量：输入 9 / 输出 1 tokens
```

### 该重试和不该重试

| 错误 | 重试有用吗 | 理由 |
|---|---|---|
| 连接失败 / 超时 | 有用 | 多半是瞬时抖动 |
| 429 限流 | 有用 | 但要听 `Retry-After` |
| 5xx | 有用 | 服务端临时故障 |
| 401 鉴权失败 | **没用** | Key 不会自己变对 |
| 400 参数错误 | **没用** | 同样的请求还是同样的错 |
| 404 模型不存在 | **没用** | 名字错了就是错了 |

对最后一类反复重试，等于让用户多等、让你多花钱。

### 四个细节

1. **SDK 自带重试**：`max_retries` 默认 2 次，遇到连接错误/408/409/429/5xx 自动退避。自己写循环时务必传 `max_retries=0`，否则 2 × 4 = 最坏 8 次请求。
2. **默认超时 10 分钟**：`timeout` 不设的话，一次卡住的请求能挂十分钟。聊天界面建议 30s 左右。
3. **`Retry-After` 优先**：服务端说等多久就等多久，比自己猜的指数退避更准（示例里 0.3s 而不是 2s）。
4. **流式不能中途重试**：已经吐给用户的字收不回来，重试会重复输出。只能在"还没收到第一个字"时重试。

## 第 5 步：稳定输出 JSON

```powershell
python 05_json.py
```

任务：把一段散乱的客服留言抽成结构化字段（姓名、电话、订单号、紧急度……）。

实测输出：

```
--- 模型原始输出 ---
{"name":"张伟","phone":"13800001234","email":"zhangwei@example.com","order_id":"A20261015001",
 "product":"27 寸显示器","issue":"下单后至今未发货，客户要求今天之内给答复，否则退款",
 "urgency":"high","refund_requested":true}
--------------------

校验通过，解析结果：

  name               '张伟'
  phone              '13800001234'
  email              'zhangwei@example.com'
  order_id           'A20261015001'
  product            '27 寸显示器'
  issue              '下单后至今未发货，客户要求今天之内给答复，否则退款'
  urgency            'high'
  refund_requested   True

用量：输入 302 / 输出 67 tokens
```

修复回路实测（故意注入一个坏输出，看它能不能自己改回来）：

```
--- 模型原始输出 ---
{"urgency":"urgent"}
--------------------

校验没通过：
  - 缺少字段 name
  - urgency 必须是 low/medium/high，实际是 'urgent'
  ...

把问题回给模型，让它修一次...

--- 修复后的输出 ---
{"name":"张伟","phone":"13800001234","order_id":"A20261015001",... "urgency":"high",...}
--------------------

校验通过，解析结果：……
用量：输入 408 / 输出 67 tokens
```

### 两个必须记住的坑

1. **只设 `response_format` 不够**。官方文档明确警告：如果提示词里没要求输出 JSON，模型可能一直生成空白字符直到撞上 token 上限，表现为"请求卡住、不返回"。所以提示词里必须写清"只输出 JSON，不要解释"。
2. **JSON 合法 ≠ 字段正确**。`json_object` 只保证能 `json.loads`，不保证字段名、类型、枚举值符合你的期待。实测里模型把 `138-0000-1234` 转成了 `13800001234`（这次是对的），但换个模型或换个问法就未必，必须用代码校验兜住。

### 所以流程应该是四步

```
提示词要 JSON  →  json_object 兜底  →  解析 + 校验  →  不合格回炉一次
```

校验不过还硬把数据喂给下游，比直接报错糟糕得多——错误数据会静默扩散。

## 第 6 步：工具调用

```powershell
python 06_tools.py            # 正常流程
python 06_tools.py --break    # 复现一个真实的 400
```

模型不再只输出文字，而是可以"申请调用函数"，由你在本地真正执行，再把结果发回去。

实测输出：

```
[第 1 轮] 模型申请调用 2 个工具
          （本轮思考 84 字，已随消息回存）
  模型想调用：get_weather({"location": "杭州", "date": "2026-10-26"})
  本地执行结果：{"location": "杭州", "date": "2026-10-26", "weather": "多云", "temp": "17~24℃"}
  模型想调用：get_order_status({"order_id": "A20261015001"})
  本地执行结果：{"order_id": "A20261015001", "status": "已打包待揽收", "carrier": "顺丰", ...}

[第 2 轮] 最终回答：
您好，杭州明天（2026-10-26）的天气和订单 A20261015001 的物流状态如下：……

用量：输入 643 / 输出 147 tokens
```

### 一次提问 = 多轮请求

```
你发消息 → 模型说"我要调 get_weather({...})"
        → 你本地执行，把结果用 role=tool 发回去
        → 模型可能再申请别的工具
        → 直到它不再申请，给出最终回答
```

**模型从来不执行你的代码**，它只生成"调用申请"。真正的函数在你这边跑，所以：参数必须自己解析校验、能碰什么数据由你决定、工具报错也要作为结果回给模型（让它自己弥补），而不是直接抛给用户。

### 实测复现的 400

```
实验：thinking 模式 + tool_choice="required"
[错误] 400：Thinking mode does not support this tool_choice
```

两种修法：改回 `tool_choice="auto"`（推荐），或者

```python
extra_body={"thinking": {"type": "disabled"}}   # 确实要强制调用时才关思考
```

### 文档说的和实测不一样

| 说法 | 文档 | 实测（当前版本） |
|---|---|---|
| 带 `tools` 时漏传 `reasoning_content` → 400 | 明确要求必须回传 | **没有报错**，请求正常完成 |

我按文档写了个 `--break` 想复现，结果连 `reasoning_effort="max"` 都能过。所以现在的处理是：**仍然按文档回传**，理由不是怕报错，而是不回传等于让模型丢掉上一轮的推理过程，多轮工具调用容易接不上。文档偏保守，按它做跨版本更安全。

另外注意：模型不知道今天几号。脚本里把 `datetime.date.today()` 写进 system 提示，否则它没法把"明天"换算成具体日期（第一次跑的时候它直接反问用户要日期）。

## 第 7 步：包成 HTTP 接口 + 网页

```powershell
python 07_server.py            # 默认 8010 端口
python 07_server.py --port 9000
```

然后浏览器打开 <http://127.0.0.1:8010>，就能直接打字对话了。

用标准库 `http.server` 实现，**零依赖**。这样做不是为了省事，而是因为用它能把"流式"在 HTTP 层到底是什么看清楚：服务端往同一个响应里一行行写 `data:`，浏览器读一块渲染一块。FastAPI 只是把同一件事包装得更方便。

### 接口只有一个

```
POST /chat
请求：{"messages": [{"role": "user", "content": "你好"}]}
响应：text/event-stream
  data: {"delta": "你"}
  data: {"delta": "好"}
  data: {"usage": {"in": 27, "out": 47}}
  data: [DONE]
```

实测（用脚本模拟浏览器发的请求，时间戳是真跑出来的）：

```
GET / -> 200 text/html; charset=utf-8 6546 bytes
标题: DeepSeek 流式对话练习

POST /chat -> 200 text/event-stream; charset=utf-8
   0.75s  data: {"delta": "SS"}
   0.86s  data: {"delta": "（"}
   1.02s  data: {"delta": "。"}
   ...
   1.19s  data: {"usage": {"in": 27, "out": 47}}
   1.19s  data: [DONE]

共收到 49 行 SSE，总耗时 1.19s
```

输入校验也验过：

```
[空 messages] (400, '{"error": "请求格式不对：messages 必须是非空数组"}')
[role 非法]    (400, '{"error": "请求格式不对：每条消息需要 role(user/assistant) 和字符串 content"}')
[不是 JSON]    (400, '{"error": "请求格式不对：Expecting value: line 1 column 1 (char 0)"}')
```

### 服务端不存历史

浏览器把整段对话存在 `history` 数组里，每次 POST 全发过来——就是第 3 步那个结论换了个地方成立。好处是服务端可以随意重启、水平扩容；代价是历史越长请求越大，和前面算过的账一模一样。

### 想换成 FastAPI

代码可以短很多，把 `do_POST` 换成：

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()

@app.post("/chat")
def chat(payload: dict):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + payload["messages"]
    def gen():
        for event in stream_reply(messages):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")
```

启动：`pip install fastapi uvicorn`，然后 `uvicorn 07_server_fastapi:app --port 8010`。

> 这段没在本文档的环境里跑过（沙箱装不上 FastAPI），写法是标准用法，你本机装好依赖后应该能直接跑；有差异以 FastAPI 文档为准。

### 看流式最直观的方式

打开页面后按 F12 → Network → 点那个 `/chat` 请求 → Response 标签。你会看到文字是一个一个往里加的，而不是一次性出现——这就是 SSE。

## 第 8 步：一般做法（服务端会话版）

第 7 步是"能跑通原理"的最小版。真实项目里的常规做法是这一版：

```powershell
python 08_app.py  # 或 --port 9000
```

然后打开 <http://127.0.0.1:8020>。

| 项目 | 07 最小版 | 08 一般做法 |
|---|---|---|
| 对话历史 | 浏览器存，每次全发过来 | **服务端存**，浏览器只发这一句 |
| 用户识别 | 无 | Cookie 里的 session id（HttpOnly） |
| 谁能用 | 谁都能用 | 可配访问口令，不配则仅本机可用 |
| 防刷 | 无 | 每会话每分钟上限，超了返回 429 |
| 长对话 | 无限增长 | 只带最近 N 条，且截断点必须落在 user 上 |

### 实测（口令 test123、限流 2 次/分钟）

```
1) GET / -> 200
2) GET /api/me -> {'auth_required': True, 'history': 0, 'model': 'deepseek-flash'}
3) 无口令 POST /chat -> (401, '{"error": "需要访问口令（页面右上角输入）"}')
4) 有口令 第一轮 -> (200, '好的，小林。')
5) 有口令 第二轮 -> (200, '你叫小林。')          ← 历史真的存在服务端
6) 第三轮（超限流） -> (429, '{"error": "太频繁了，58 秒后再试"}')
7) GET /api/me -> {'history': 4}
8) POST /reset -> {"ok": true, "history": 0}
9) GET /api/me -> {'history': 0}
```

历史裁剪单独验过（`limit=3` 时取最后 3 条，发现首条是 assistant 就再丢一条）：

```
limit= 0 -> []
limit= 3 -> [('u', 'q5'), ('a', 'a5')]
limit= 4 -> [('u', 'q4'), ('a', 'a4'), ('u', 'q5'), ('a', 'a5')]
```

### 为什么截断点必须落在 user 上

如果窗口恰好从一条 assistant 回答开始，模型会收到"一个没有提问的回答"，接话会莫名其妙。所以要往前挪到最近的 user。

### 三个开关（写在 `.env` 里）

```
ACCESS_TOKEN=           # 留空 = 不校验；设了就要在页面输入
RATE_LIMIT_PER_MIN=20   # 每会话每分钟上限
MAX_HISTORY=12          # 每次最多带多少条历史
```

### 这一版离上线还差什么

1. **会话存在内存里**，重启就没了。生产要换成 Redis 或数据库。
2. **单进程**，多开几个实例会话就不共享了。
3. **限流是按会话算的**，换一个浏览器就重置。生产要按 IP 或用户算。
4. **没有 HTTPS**，口令是明文传输的，只能在 localhost 用。
5. `ThreadingHTTPServer` 只适合练习，真实流量下要换成带反向代理的方案（Nginx/Caddy 转发到 uvicorn/gunicorn）。

## 报错对照

| 现象 | 原因 | 处理 |
|---|---|---|
| `没有找到 DEEPSEEK_API_KEY` | `.env` 没填，或填进了注释行 | 按提示填 |
| 401 鉴权失败 | Key 错、被删、带空格或引号 | 重新复制 Key |
| 402 | 余额不足 | 平台充值 |
| 404 | 模型名写错 | 用 `deepseek-flash` 或 `deepseek-v4-pro` |
| `400 Thinking mode does not support this tool_choice` | thinking 模式下用了 `required` 或指定函数 | 改 `auto`，或关掉 thinking |
| 400 提到 `reasoning_content` | 带 `tools` 却没回传思考内容（文档要求；当前版本实测不报错） | 仍建议把 `reasoning_content` append 回 history |
| 429 | 请求太频繁 | 听 `Retry-After`，别硬刷 |
| 400 请求格式不对 | 客户端发的 JSON 不符合约定 | 看错误里的具体原因，服务端已做校验 |
| `UnicodeEncodeError ... '\udcab'` | 管道传入的中文被错误解码 | 加 `-X utf8`，或直接用终端输入 |
| 网络没连上 | 代理 / 防火墙 | 确认命令行网络；DeepSeek 一般不需要代理 |

## 八步之后

| 步 | 文件 | 学会了 |
|---|---|---|
| 1 | `01_first_call.py` | 打通接口、三个核心参数、用量统计 |
| 2 | `02_stream.py` | 流式输出、首字延迟、思考 token 的成本 |
| 3 | `03_multi_turn.py` | 上下文是"每次重发历史"、成本随轮数涨 |
| 4 | `04_retry.py` | 什么该重试、退避与 `Retry-After`、超时 |
| 5 | `05_json.py` | 结构化输出、校验、修复回路 |
| 6 | `06_tools.py` | 工具调用、多轮工具循环、`tool_choice` 限制 |
| 7 | `07_server.py` + `static/index.html` | HTTP + SSE、前后端配合、服务端不存状态 |
| 8 | `08_app.py` + `static/app.html` | 服务端会话、Cookie、口令、限流、历史裁剪 |

**日常开发从 `08_app.py` 开始**；`07_server.py` 留着是为了看懂 SSE 的裸机制。

## 继续练什么

1. 把 08 的会话**搬到 SQLite**：重启后对话还在，顺便支持多标签页。
2. 给 05 的抽取套一层**批量处理 + 逐条重试**，处理一百条留言。
3. 给 06 加一个 **`get_current_date` 工具**，让模型自己解决"明天是哪天"。
4. 做一次**成本核算**：同一任务分别用 `deepseek-flash` 和 `deepseek-v4-pro` 跑，比较价格与质量。
5. **部署**：Nginx 反代 + HTTPS + 按 IP 限流，再上服务器。现在的口令是明文、会话在内存里，只适合本地。
