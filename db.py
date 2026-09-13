# -*- coding: utf-8 -*-
"""去重：URL 指纹存 sqlite，已处理过的资讯不再重复进 LLM

三种状态：
  pending   抓到了、判过但还不确定（LLM 失败/超时）——下轮还值得再判一次
  confirmed LLM 确认相关，已推送——永不重来
  dropped   LLM 明确判为无关，或重试次数用完——不再浪费 token

只算「不确定」，不算「无关」。无关的条目后续出了进展（同题材新报道）会带着
新 URL 进来，重新走一遍判断——这才是想要的行为。
"""
import hashlib
import sqlite3

import config

#: pending 条目最多判几次，超过就当无关丢掉（防止个别坏条目无限重试烧 token）
MAX_ATTEMPTS = 3

#: pending 条目超过这个时长就作废。LLM 长时间故障期间积压的资讯，恢复后不该
#: 当成「刚刚发生」推给用户——过期的资讯比不推更糟。
MAX_AGE_HOURS = 6

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS seen("
    "url_hash TEXT PRIMARY KEY, url TEXT, status TEXT DEFAULT 'pending',"
    "attempts INTEGER DEFAULT 0,"
    "added_at TEXT DEFAULT (datetime('now','localtime')))"
)


def _hash(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(_SCHEMA)
    # 旧库补列（第一次升级时会走这里）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(seen)")}
    if "status" not in cols or "attempts" not in cols:
        if "status" not in cols:
            conn.execute("ALTER TABLE seen ADD COLUMN status TEXT DEFAULT 'pending'")
            # 旧库只有「已处理」一种语义，直接归为终态，避免升级后把已推过的资讯再推一遍
            conn.execute("UPDATE seen SET status='confirmed'")
        if "attempts" not in cols:
            conn.execute("ALTER TABLE seen ADD COLUMN attempts INTEGER DEFAULT 0")
        conn.commit()          # 建表/加列/回填都要显式提交，否则连接关闭时被回滚
    return conn


def is_new(url: str) -> bool:
    """该 URL 是否还需要处理：没见过的，或判过但尚未定论且没过期的"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT status, attempts,"
            " (julianday('now','localtime') - julianday(added_at)) * 24"
            " FROM seen WHERE url_hash=?",
            (_hash(url),),
        ).fetchone()
        if row is None:
            return True
        status, attempts, age_h = row
        if status != "pending" or (attempts or 0) >= MAX_ATTEMPTS:
            return False
        return (age_h or 0) < MAX_AGE_HOURS
    finally:
        conn.close()


def mark_attempt(url: str):
    """LLM 答了但漏掉这一条，计入该条目的尝试次数。

    次数用完就落定为 dropped，不留 pending 尾巴——否则每轮都会被重新捞出来。
    整批调用失败（LLM 宕机）不走这里，那种情况不算条目的问题，见 news.py。
    """
    conn = _connect()
    try:
        h = _hash(url)
        row = conn.execute(
            "SELECT attempts FROM seen WHERE url_hash=? AND status='pending'", (h,)
        ).fetchone()
        n = (row[0] or 0) + 1 if row else 1
        status = "dropped" if n >= MAX_ATTEMPTS else "pending"
        conn.execute(
            "INSERT INTO seen(url_hash, url, status, attempts) VALUES(?,?,?,?)"
            " ON CONFLICT(url_hash) DO UPDATE SET status=?, attempts=?"
            " WHERE seen.status='pending' AND seen.attempts < ?",
            (h, url, status, n, status, n, MAX_ATTEMPTS),
        )
        conn.commit()
    finally:
        conn.close()


def mark_done(url: str, status: str = "dropped"):
    """给出定论：confirmed=已推送 / dropped=无关"""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO seen(url_hash, url, status, attempts) VALUES(?,?,?, 1)"
            " ON CONFLICT(url_hash) DO UPDATE SET status = ?, attempts = 1",
            (_hash(url), url, status, status),
        )
        conn.commit()
    finally:
        conn.close()
