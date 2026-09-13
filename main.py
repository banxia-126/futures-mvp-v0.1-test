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
import sys
import threading
import time

import config
import market
import news


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

    # 默认：资讯轮询放后台线程，行情监控在主线程
    if not config.TQ_USER:
        print("[main] 未配置快期账号，只启动资讯模块")
        news_loop()
        return
    threading.Thread(target=news_loop, daemon=True).start()
    market.live()


if __name__ == "__main__":
    main()
