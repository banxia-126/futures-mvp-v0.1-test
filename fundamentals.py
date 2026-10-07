# -*- coding: utf-8 -*-
"""基本面数据：落库优先（仓单 / 库存），暂不推送。

已通的数据源（都免费，2026-10-07 实测）：
- 郑商所仓单日报：每日静态 xlsx，无鉴权；节假日当天没有文件属正常，向前找最近一个交易日
- 东财期货库存（akshare futures_inventory_em）：一次返回整段历史，天然完成首次回填

落库到 fundamentals.db 的 series 表（indicator+date 唯一，重跑幂等）。
节奏：由 main.py 挂后台线程，每天 FUND_FETCH_HOUR 之后跑一次；失败当天不重试
（东财下次调用带全历史能自愈缺失日；仓单只追最新一天，中间缺日不追）。

未通：99期货（返回非 JSON，跳过）；生意社现货价格（网页反爬 HW_CHECK，跳过）。
后续步骤：新闻文本抽取开工率/产量/港口库存、周度卡片、变化提醒。
"""
import os
import sqlite3
import time
from datetime import datetime, timedelta
from io import BytesIO

import pandas as pd
import requests

import config

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fundamentals.db")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

CZCE_XLSX = "http://www.czce.com.cn/cn/DFSStaticFiles/Future/{y}/{d}/FutureDataWhsheet.xlsx"


def _save(indicator: str, points: list):
    """写 [(date, value), ...]；同 (indicator, date) 覆盖更新（当天数据可能被修正）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS series (
            indicator  TEXT NOT NULL,
            date       TEXT NOT NULL,
            value      REAL,
            fetched_at TEXT,
            PRIMARY KEY (indicator, date))""")
        conn.executemany(
            "INSERT INTO series(indicator, date, value, fetched_at) VALUES(?,?,?,?) "
            "ON CONFLICT(indicator, date) DO UPDATE SET "
            "value=excluded.value, fetched_at=excluded.fetched_at",
            [(indicator, d, v, now) for d, v in points])
        conn.commit()
    finally:
        conn.close()


def _latest_stored_date(indicator: str):
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT MAX(date) FROM series WHERE indicator=?", (indicator,)).fetchone()
        return datetime.strptime(row[0], "%Y-%m-%d").date() if row and row[0] else None
    except sqlite3.OperationalError:          # 表还不存在
        return None
    finally:
        conn.close()


def _czce_one_day(day) -> dict | None:
    """某天的郑商所仓单日报里甲醇合计。文件不存在（节假日/未发布）返回 None。"""
    url = CZCE_XLSX.format(y=day.strftime("%Y"), d=day.strftime("%Y%m%d"))
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
    except Exception:
        return None
    if r.status_code != 200 or len(r.content) < 10000:
        return None
    try:
        df = pd.read_excel(BytesIO(r.content), header=None)
        col0 = df.iloc[:, 0].astype(str)
        starts = col0[col0.str.startswith("品种：甲醇")].index
        if not len(starts):
            return None

        def num(v):
            v = pd.to_numeric(v, errors="coerce")
            return float(v) if pd.notna(v) else 0.0

        # 一块仓单按仓库分行，同一仓库可能跨多行（续行没有仓库编号）。
        # 所以不能按「有编号的行」求和（会漏掉续行的量）——只认汇总行：
        # 优先「总计」行；没有就累加各仓库「小计」行。2026-10-07 用 09-30
        # 文件校准过：与小计/总计一致，且总计=东财序列同值。
        receipt = forecast = 0.0
        found_total = False
        for i in range(int(starts[0]) + 1, len(df)):
            code = str(df.iloc[i, 0]).strip()
            if code.startswith("品种"):                # 下一个品种块，收工
                break
            if code == "总计":
                receipt = num(df.iloc[i, 3]) + num(df.iloc[i, 4])
                forecast = num(df.iloc[i, 6])
                found_total = True
                break
            if code == "小计":
                receipt += num(df.iloc[i, 3]) + num(df.iloc[i, 4])
                forecast += num(df.iloc[i, 6])
        if not found_total and receipt == 0 and forecast == 0:
            return None                                # 既没总计也没小计，视为解析失败
        return {"receipt": receipt, "forecast": forecast}
    except Exception:
        return None


def fetch_czce_receipts():
    """找最近一个已发布的仓单日报并落库；返回落库日期或 None。

    从今天向前逐日找，找到即止；已存过的最早日期是下界（不追中间缺日）。
    每天只由后台线程触发一次，不构成反复重试。
    """
    today = datetime.now().date()
    latest = _latest_stored_date("czce_ma_receipt")
    days_back = min((today - latest).days, 12) if latest else 12
    for back in range(days_back + 1):
        day = today - timedelta(days=back)
        if latest and day <= latest:
            break                     # 更新的都试过且没有，剩下的都存过
        data = _czce_one_day(day)
        if data is None:
            continue
        d = day.strftime("%Y-%m-%d")
        _save("czce_ma_receipt", [(d, data["receipt"])])
        _save("czce_ma_forecast", [(d, data["forecast"])])
        return d
    return None


def fetch_em_inventory() -> int:
    """东财-甲醇期货库存（整段历史，天然回填）。返回写入条数，失败 0。"""
    try:
        import akshare as ak          # 重依赖，惰性导入；只在落库线程里用到
        df = ak.futures_inventory_em(symbol="MA")
    except Exception as e:
        print(f"[fund] 东财库存抓取失败: {e}")
        return 0
    for ind, col in (("em_ma_inventory", "库存"), ("em_ma_inventory_chg", "增减")):
        points = [(str(d)[:10], float(v)) for d, v in zip(df["日期"], df[col])]
        _save(ind, points)
    return len(df)


def update_all():
    """跑一轮全部源。后台线程每日调用；`python fundamentals.py` 可手动触发（首次回填）。"""
    czce = fetch_czce_receipts()
    em_n = fetch_em_inventory()
    print(f"[fund] 落库：仓单 -> {czce or '无新发布'}；东财库存 {em_n} 条")


def loop():
    """后台线程：每天 FUND_FETCH_HOUR 之后跑一次（启动时若已过点也补一次）。"""
    done_date = None
    while True:
        now = datetime.now()
        if now.hour >= config.FUND_FETCH_HOUR and done_date != now.date():
            try:
                update_all()
            except Exception as e:
                print(f"[fund] 落库异常: {e}")
            done_date = now.date()          # 失败也记，当天不再重试
        time.sleep(600)


if __name__ == "__main__":
    update_all()
