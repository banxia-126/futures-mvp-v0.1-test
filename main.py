# -*- coding: utf-8 -*-
"""期货信号 MVP 入口（暂无任何策略逻辑，先跑通两条链路）

用法：
  python main.py --news-once     跑一轮资讯抓取→总结→推送，然后退出（验证资讯链路）
  python main.py --market-test   立即抓一次行情快照推送，然后退出（验证行情链路，周末可用）
  python main.py                 常驻运行：行情开盘/收盘快照 + 资讯每15分钟一轮
  python main.py --news-only     只跑资讯轮询
  python main.py --market-only   只跑行情监控
"""
import argparse
import msvcrt
import os
import sys
import threading
import time

import config
import fundamentals
import market
import news

#: 常驻模式的单实例锁文件（锁随进程退出自动释放，不用清理）
_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mvp.lock")


def _acquire_single_instance():
    """常驻模式防双开：多实例会重复推送，且多个 TqApi 会话会互相踢下线。

    用文件锁而不是查进程：崩溃退出时锁自动释放，不会留假阳性（曾误判过）。
    返回需一直保持引用的文件对象（放局部变量即可），拿不到锁返回 None。
    """
    f = open(_LOCK_FILE, "a+b")
    f.seek(0)
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return None
    return f


def news_loop():
    while True:
        try:
            news.run_cycle()
        except Exception as e:
            print(f"[news] 轮询异常: {e}")
        time.sleep(config.NEWS_INTERVAL)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="期货信号 MVP")
    ap.add_argument("--news-once", action="store_true", help="跑一轮资讯并退出")
    ap.add_argument("--market-test", action="store_true", help="推一次行情快照并退出")
    ap.add_argument("--news-only", action="store_true", help="只跑资讯轮询")
    ap.add_argument("--market-only", action="store_true", help="只跑行情监控")
    args = ap.parse_args()

    if args.news_once:
        news.run_cycle()
        return
    if args.market_test:
        market.market_test()
        return
    if args.market_only:
        market.live()
        return
    if args.news_only:
        news_loop()
        return

    # 常驻模式加单实例锁（一次性验证命令 --news-once/--market-test 不受限）
    lock = _acquire_single_instance()
    if lock is None:
        print("[main] 已有常驻实例在运行，本次退出（要重启请先停掉旧实例）")
        return

    # 基本面落库线程（仓单/库存；不依赖行情和资讯，节假日也安全）
    threading.Thread(target=fundamentals.loop, daemon=True).start()

    # 默认：资讯轮询放后台线程，行情监控在主线程
    if not config.TQ_USER:
        print("[main] 未配置快期账号，只启动资讯模块")
        news_loop()
        return
    threading.Thread(target=news_loop, daemon=True).start()
    market.live()


if __name__ == "__main__":
    main()
