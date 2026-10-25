"""离线测试：不联网、不花钱、不需要 API Key。

跑：
    python -B -m unittest discover -s tests -v

测的是"纯逻辑"——字段校验、JSON 解析、历史裁剪、退避计算、字符清洗。
真实请求那部分不测，靠手跑，记录在 docs/STEPS.md。

为什么模块名带数字还能 import：importlib 对模块名不做标识符校验，
import_module("04_retry") 能正常工作。
"""

import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load(name):
    return importlib.import_module(name)


GOOD_FIELDS = {
    "customer": "张伟",
    "contact": "13800001234",
    "order_id": "A20261015001",
    "product": "27 寸显示器",
    "issue": "下单后未发货",
    "urgency": "high",
    "wants_refund": True,
}


class FieldValidationTest(unittest.TestCase):
    """09 的抽取校验：JSON 合法 ≠ 字段合格。"""

    def setUp(self):
        self.mod = load("09_ticket_assistant")

    def test_good_fields_pass(self):
        self.assertEqual(self.mod.validate_fields(GOOD_FIELDS), [])

    def test_missing_field_is_reported(self):
        data = dict(GOOD_FIELDS)
        del data["order_id"]
        problems = self.mod.validate_fields(data)
        self.assertTrue(any("order_id" in p for p in problems), problems)

    def test_null_values_are_allowed(self):
        data = dict(GOOD_FIELDS, order_id=None, customer=None)
        self.assertEqual(self.mod.validate_fields(data), [])

    def test_bad_urgency_rejected(self):
        data = dict(GOOD_FIELDS, urgency="urgent")
        problems = self.mod.validate_fields(data)
        self.assertTrue(any("urgency" in p for p in problems), problems)

    def test_bool_as_string_rejected(self):
        data = dict(GOOD_FIELDS, wants_refund="true")
        problems = self.mod.validate_fields(data)
        self.assertTrue(any("wants_refund" in p for p in problems), problems)

    def test_non_object_rejected(self):
        problems = self.mod.validate_fields(["not", "an", "object"])
        self.assertEqual(len(problems), 1)


class JsonParseTest(unittest.TestCase):
    """05 的解析 + 校验：坏 JSON 要被拦住，脏字段也要被拦住。"""

    def setUp(self):
        self.mod = load("05_json")

    def test_invalid_json(self):
        data, problems = self.mod.parse_and_check("this is not json")
        self.assertIsNone(data)
        self.assertIn("不是合法 JSON", problems[0])

    def test_empty_output(self):
        data, problems = self.mod.parse_and_check("")
        self.assertIsNone(data)
        self.assertTrue(problems)

    def test_missing_fields(self):
        _, problems = self.mod.parse_and_check('{"urgency": "high"}')
        self.assertTrue(any("缺少字段" in p for p in problems), problems)

    def test_phone_with_dashes(self):
        raw = (
            '{"name":"x","phone":"138-0000","email":null,"order_id":null,'
            '"product":null,"issue":null,"urgency":"high","refund_requested":true}'
        )
        _, problems = self.mod.parse_and_check(raw)
        self.assertTrue(any("横线" in p for p in problems), problems)

    def test_clean_payload_passes(self):
        raw = (
            '{"name":"张伟","phone":"13800001234","email":null,"order_id":null,'
            '"product":null,"issue":null,"urgency":"high","refund_requested":true}'
        )
        data, problems = self.mod.parse_and_check(raw)
        self.assertEqual(problems, [])
        self.assertEqual(data["name"], "张伟")


class HistoryWindowTest(unittest.TestCase):
    """08 的历史裁剪：只留最近 N 条，且必须从 user 开始。"""

    def setUp(self):
        self.mod = load("08_app")
        self.messages = []
        for i in range(6):
            self.messages.append({"role": "user", "content": f"q{i}"})
            self.messages.append({"role": "assistant", "content": f"a{i}"})

    def test_limit_zero(self):
        self.assertEqual(self.mod.history_window(self.messages, 0), [])

    def test_odd_limit_drops_leading_assistant(self):
        window = self.mod.history_window(self.messages, 3)
        self.assertEqual([m["content"] for m in window], ["q5", "a5"])
        self.assertEqual(window[0]["role"], "user")

    def test_even_limit_keeps_pairs(self):
        window = self.mod.history_window(self.messages, 4)
        self.assertEqual([m["content"] for m in window], ["q4", "a4", "q5", "a5"])

    def test_never_exceeds_limit(self):
        for limit in range(0, 13):
            self.assertLessEqual(len(self.mod.history_window(self.messages, limit)), limit)


class RetryDelayTest(unittest.TestCase):
    """04 的退避：有 Retry-After 就听服务端，没有就指数退避 + 抖动。"""

    def setUp(self):
        self.mod = load("04_retry")

    def _rate_limit(self, retry_after):
        import httpx2
        from openai import RateLimitError

        request = httpx2.Request("POST", "https://api.deepseek.com/chat/completions")
        headers = {"retry-after": retry_after} if retry_after is not None else {}
        response = httpx2.Response(429, headers=headers, request=request, json={"error": {"message": "x"}})
        return RateLimitError("rate limited", response=response, body=None)

    def test_honours_retry_after(self):
        delay, reason = self.mod.retry_delay(self._rate_limit("0.3"), 1, 0.5, 8.0)
        self.assertAlmostEqual(delay, 0.3, places=6)
        self.assertIn("0.3", reason)

    def test_retry_after_is_capped(self):
        delay, _ = self.mod.retry_delay(self._rate_limit("999"), 1, 0.5, 8.0)
        self.assertEqual(delay, 8.0)

    def test_backoff_grows_and_jitters(self):
        for attempt, base in [(1, 0.5), (2, 1.0), (3, 2.0)]:
            delay, reason = self.mod.retry_delay(self._rate_limit(None), attempt, 0.5, 8.0)
            self.assertGreaterEqual(delay, base * 0.8 - 1e-9)
            self.assertLessEqual(delay, base * 1.2 + 1e-9)
            self.assertIn("指数退避", reason)


class SanitizeTest(unittest.TestCase):
    """common.sanitize：非法代理对要在入口清掉，不能等到发请求才炸。"""

    def setUp(self):
        self.mod = load("common")

    def test_normal_text_untouched(self):
        self.assertEqual(self.mod.sanitize("你好，世界"), "你好，世界")

    def test_lone_surrogate_replaced(self):
        broken = "abc\udcabdef"
        cleaned = self.mod.sanitize(broken)
        # errors="replace" 会把无法编码的字符换成 "?"，所以是 7 个字符而不是 6 个
        self.assertEqual(cleaned, "abc?def")
        self.assertNotIn("\udcab", cleaned)
        cleaned.encode("utf-8")  # 不再抛 UnicodeEncodeError 即通过


class ToolImplTest(unittest.TestCase):
    """09 的工具：返回值必须是能解析的 JSON，且带上入参的订单号。"""

    def setUp(self):
        self.mod = load("09_ticket_assistant")

    def test_returns_json_with_order_id(self):
        import json

        result = self.mod.get_order_status("A20261015001")
        data = json.loads(result)
        self.assertEqual(data["order_id"], "A20261015001")
        self.assertIn("status", data)

    def test_tool_registered(self):
        self.assertIn("get_order_status", self.mod.TOOL_IMPL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
