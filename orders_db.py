"""订单库：一个 SQLite 文件，替代原来写死的示例数据。

真实项目里这里应该换成订单服务或业务库，但**接口不变**：
上层只调用 `lookup(order_id)`，拿回一个 dict 或 None。
所以以后换成 HTTP 调用，`09_ticket_assistant.py` 一行都不用改。

用法：
    python orders_db.py --seed     建表 + 写入示例订单
    python orders_db.py --list     看看库里有什么
    python orders_db.py --init     只建表

数据库文件默认 ./orders.db，可用环境变量 ORDERS_DB 指定别的路径（测试用）。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).parent / "orders.db"


def db_path() -> Path:
    return Path(os.environ.get("ORDERS_DB") or DEFAULT_DB)


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path) if path else db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init(conn: sqlite3.Connection) -> None:
    """建表。幂等，重复调用不会报错。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            order_id   TEXT PRIMARY KEY,
            waybill    TEXT,          -- 运单号，物流 API 要的是这个，不是订单号
            carrier    TEXT,          -- 快递公司
            status     TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def lookup(order_id: str, path: Path | str | None = None) -> dict | None:
    """按订单号查一条记录。查不到返回 None。

    注意这里是**精确匹配**，不是模糊搜索——真实系统里这一点很重要，
    否则客户输错一位数字，可能匹配到别人的订单。
    """
    conn = connect(path)
    try:
        init(conn)  # 表不存在就建，避免第一次查询直接炸
        row = conn.execute(
            "SELECT order_id, waybill, carrier, status, updated_at FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


SEED_ROWS = [
    ("A20261015001", "SF1234567890", "顺丰", "已打包待揽收", "2026-10-26 09:20"),
    ("A20261015002", "YT9876543210", "圆通", "运输中，已到达杭州转运中心", "2026-10-26 07:40"),
    ("A20261015003", "JD0001112223", "京东物流", "已签收", "2026-10-24 18:05"),
]


def seed(path: Path | str | None = None) -> int:
    conn = connect(path)
    try:
        init(conn)
        conn.executemany(
            "INSERT OR REPLACE INTO orders (order_id, waybill, carrier, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            SEED_ROWS,
        )
        conn.commit()
        return len(SEED_ROWS)
    finally:
        conn.close()


def advance(order_id: str, status: str, updated_at: str, path: Path | str | None = None) -> bool:
    """手动推进一条订单的状态，用来演示"数据会变"。返回是否命中。"""
    conn = connect(path)
    try:
        init(conn)
        cur = conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?",
            (status, updated_at, order_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def main() -> int:
    # Windows 控制台默认可能是 GBK，中文会乱码；这里和 common.configure_stdout 做同一件事，
    # 但不依赖 common（订单库不该为了打印去 import openai）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = sys.argv[1:]

    if "--seed" in args:
        count = seed()
        print(f"已写入 {count} 条订单 -> {db_path()}")
        return 0

    if "--init" in args:
        conn = connect()
        try:
            init(conn)
        finally:
            conn.close()
        print(f"已建表 -> {db_path()}")
        return 0

    if "--list" in args:
        conn = connect()
        try:
            init(conn)
            rows = [dict(r) for r in conn.execute("SELECT * FROM orders ORDER BY order_id")]
        finally:
            conn.close()
        if not rows:
            print("库里没有数据，先跑 --seed")
            return 0
        for row in rows:
            print(f"{row['order_id']}  {row['carrier']:<6} {row['status']}  ({row['updated_at']})")
        return 0

    print("用法：")
    print("  python orders_db.py --seed    建表并写入示例订单")
    print("  python orders_db.py --list    列出当前所有订单")
    print("  python orders_db.py --init     只建表")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
