# 工单助手

![tests](https://github.com/xxh666umr/deepseek-api-practice/actions/workflows/tests.yml/badge.svg)

> 仓库名沿用了早期的 `deepseek-api-practice`；内容已整理成这个应用，底层八个能力模块仍在仓库里。

把一段口语化的客户留言，变成一张结构化工单和一条能直接发出去的客服回复。

```
粘贴留言  →  抽取字段  →  查订单状态  →  流式生成回复草稿
```

单文件后端（Python 标准库）+ 一个页面。唯一外部依赖是 `openai` SDK，模型 `deepseek-flash`。

这个仓库同时是一份可追溯的记录：`01`~`08` 是把这个应用逐层拆开的样子，每一步都保留了实测数据和踩过的坑，见 [`docs/STEPS.md`](docs/STEPS.md)。

## 它做什么

| 步骤 | 做什么 | 靠什么 |
|---|---|---|
| 抽取 | 从留言里取出姓名、电话、订单号、商品、问题、紧急度、是否要求退款 | JSON 模式 + 代码校验 |
| 查证 | 有订单号就调用工具查订单库；查不到就明说 | 工具调用 + SQLite |
| 起草 | 结合字段和查证结果，写 120 字以内的回复草稿 | 流式输出（SSE） |

## 跑起来

```bash
git clone https://github.com/xxh666umr/deepseek-api-practice.git
cd deepseek-api-practice
python -m pip install openai
cp .env.example .env          # 填入下面申请的 Key
python orders_db.py --seed    # 建本地订单库，写入 3 条示例订单
python 09_ticket_assistant.py
```

打开 <http://127.0.0.1:8030>。

Key 在 <https://platform.deepseek.com/api_keys> 申请，账户要有余额。填写时不要加引号、不要留空格：

```
DEEPSEEK_API_KEY=sk-xxxxxxxx
```

`.env` 已在 `.gitignore` 里，不会被提交。可选：`ACCESS_TOKEN=你的口令 python 09_ticket_assistant.py` 开启访问口令。

## 实测

真实一次请求（`deepseek-flash`，关闭思考模式）：

```
0.11s  stage=extract
1.28s  fields={"customer":"张伟","contact":"13800001234","order_id":"A20261015001",
               "product":"27 寸显示器","issue":"下单后至今未发货",
               "urgency":"high","wants_refund":true}
1.28s  usage  extract  输入 244 / 输出 48
1.92s  tool   get_order_status {"order_id":"A20261015001"}
              → {"status":"已打包待揽收","carrier":"顺丰","updated":"今天 09:20"}
3.83s  stage=draft
5.16s  done   草稿 109 字；draft 输入 281 / 输出 69
```

总耗时 5.16s，一次约 640 tokens。草稿内容：

> 张先生您好，抱歉让您久等。订单 A20261015001（27 寸显示器）目前状态为已打包待揽收，承运方顺丰，今早 09:20 已更新。我们会盯着揽收进度，有物流信息第一时间同步给您。退款诉求我已记录，如需办理请告知。

边界行为（都实测过）：

| 输入 | 表现 |
|---|---|
| 忘了订单号 | 不调用工具，草稿里向客户索要订单号，**不编造物流** |
| 订单号库里查不到 | 工具返回 `found: false`，草稿明说没查到，请客户补充信息 |
| 空输入 | `400 message 必须是非空字符串` |
| 口令错误 | `401 需要访问口令` |
| 超过限流 | `429 太频繁了` |

## 架构

```
浏览器 ──POST /triage──▶ Handler
                          │
                          ├─ 1. 抽取  response_format=json_object
                          │          └▶ json.loads ▶ validate_fields()
                          │
                          ├─ 2. 查证  tools=[get_order_status]，最多 3 轮
                          │          └▶ 每次调用推一个 {"tool": ...} 事件
                          │
                          └─ 3. 起草  stream=True
                                     └▶ 逐块推 {"delta": ...}

SSE 事件：{"stage"} / {"fields"} / {"tool"} / {"delta"} / {"usage"}
```

## 三个设计决定

**1. 抽取结果必须过代码校验，不能只靠提示词。**
`json_object` 只保证「能解析」，不保证字段名、类型、枚举值符合预期。`validate_fields()` 会检查七个字段是否齐全、`urgency` 是否在枚举内、`wants_refund` 是否真的是布尔值。不通过就直接报错，不把脏数据喂给下游。

**2. 订单状态只能来自工具，不能让模型猜。**
模型不知道你有没有发货。`get_order_status` 现在返回的是示例数据，但调用链是真的：模型生成参数 → 本地执行 → 结果回传 → 它才写草稿。所以客户问「到哪了」，回答里的「已打包待揽收」是有出处的，不是编的。

**3. 草稿流式，字段不流式。**
字段是给程序看的，一次性返回最简单；草稿是给人看的，边写边显示，用户 3.83 秒就看到第一个字，而不是等满 5.16 秒。

## 为什么用标准库而不是 FastAPI

换 FastAPI 代码会短一半，但这一版有意不用：

- **零依赖**：`pip install openai` 之后直接能跑，不会卡在环境上
- **看得见机制**：SSE 就是往同一个响应里一行行写 `data:`，用 `http.server` 能看清这件事；框架会把它藏起来

日常开发当然应该用框架，`docs/STEPS.md` 里给了对应的 FastAPI 写法（标注了未在本机验证）。

## 目录

```
09_ticket_assistant.py   应用后端（HTTP + SSE，标准库）
orders_db.py             订单库（SQLite）：建表、seed、按订单号精确查询
static/ticket.html       页面
common.py                共用：读 Key、建 client、流式封装、报错翻译
01_first_call.py …       底层模块，每个演示一个能力（见下）
docs/STEPS.md            逐步笔记：每步的实测输出、坑、报错对照
tests/test_core.py       离线测试（22 个）
.github/workflows/       每次 push 自动跑测试
.env.example             配置模板
```

底层模块（想读懂上面那三段是怎么来的，按顺序看）：

| 文件 | 能力 |
|---|---|
| `01_first_call.py` | 打通接口、三个核心参数、用量统计 |
| `02_stream.py` | 流式输出、首字延迟、思考 token 的成本 |
| `03_multi_turn.py` | 上下文 = 每次重发历史，成本随轮数上涨 |
| `04_retry.py` | 哪些错误该重试、指数退避、`Retry-After`、超时 |
| `05_json.py` | 结构化输出、校验、不合格回炉 |
| `06_tools.py` | 工具调用协议、多轮工具循环、`tool_choice` 限制 |
| `07_server.py` | HTTP + SSE 的裸机制，零依赖 |
| `08_app.py` | 服务端会话、Cookie、口令、限流、历史裁剪 |

## 测试

```bash
python -B -m unittest discover -s tests -v
```

27 个用例，**离线、不花钱、不需要 Key**，跑一遍不到 1 秒：

```
Ran 27 tests in 0.825s
OK
```

覆盖的是纯逻辑——这些地方出错不会报错，只会悄悄给出错结果：

| 模块 | 测什么 |
|---|---|
| `09_ticket_assistant` | 字段校验：缺字段、`urgency` 越界、布尔被写成字符串、返回的不是对象 |
| `05_json` | JSON 解析失败、字段缺失、电话没洗干净、合格数据放行 |
| `08_app` | 历史裁剪：只留最近 N 条，且必须从 `user` 开始 |
| `04_retry` | `Retry-After` 优先、上限封顶、指数退避与抖动范围 |
| `common` | 非法代理对在入口被清掉，不会留到发请求时才炸 |
| `orders_db` / 工具 | 查得到返回记录、查不到返回 `None`、少一位数字不能命中、状态可推进 |
| `09_ticket_assistant` | 工具返回值是可解析 JSON，且带上入参订单号 |

**不测什么**：真实模型调用。那部分依赖网络和模型行为，断言不稳定，靠手跑验证，记录在 [`docs/STEPS.md`](docs/STEPS.md)。CI 里也不需要任何密钥。

## 局限

写清楚，别把它当成生产系统：

- **单进程、状态在内存**：重启就丢限流计数，多开实例不共享
- **测试只覆盖纯逻辑**：模型调用、HTTP 层、页面都没测，那部分靠手跑
- **数据库是本地 SQLite**：数据由 `seed` 脚本生成，不是真实订单系统
- **无 HTTPS**：口令是明文传输，只能本地或内网用
- **限流按来源地址**：同一出口 NAT 后面的人会互相影响

## 许可

MIT
