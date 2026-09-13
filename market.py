# -*- coding: utf-8 -*-
"""行情模块（MVP，无策略）：TqSdk 订阅品种 → 推送价格快照

- market_test()：立即抓一次快照推送（周末/非交易时段也能用，验证链路）
- live()：常驻运行，断线自动重连；每日心跳 + 各交易时段开盘/收盘快照 + 价格预警

交易时段来自 config.SESSIONS，只按钟点近似（不含节假日/临时调整）。
快照在「时段边界」附近触发：每个时段的第一个 tick 推开盘，走出该时段后再来的
第一个 tick 推收盘——不依赖定时器，所以断线重连后不会漏推。

三层保障分工：
  1. 本文件：TqSdk 断线 → 10 秒后自动重连（连接级自愈）
  2. start.bat：进程崩溃退出 → 自动拉起（进程级自愈）
  3. Windows 任务计划：开机自动启动 start.bat（机器重启自愈）
"""
import math
import threading
import time
from datetime import datetime, timedelta

from tqsdk import TqApi, TqAuth

import config
import push


def _connect():
    try:
        return TqApi(auth=TqAuth(config.TQ_USER, config.TQ_PASS))
    except Exception as e:
        print(f"[market] TqSdk 登录失败：{e}")
        print("请到 shinnytech.com 注册免费快期账号，填入 config.py 的 TQ_USER/TQ_PASS")
        return None


def _num(v):
    """NaN 一律当「没这个字段」"""
    return v if v is not None and not math.isnan(v) else None


def _session_ohlc(q, label):
    """取本时段的高低价。

    q.highest/q.lowest 是「当日」字段，而夜盘（21:00-23:00）交易所还没切换交易日，
    直接取会拿到白天那场的旧值——所以夜盘时段改用与本时段开盘价的相对价。
    日盘时段两者是一回事，直接用交易所口径。
    """
    hi, lo = _num(q.highest), _num(q.lowest)
    if not (label or "").startswith(("21:", "22:", "23:", "00:")):
        return hi, lo
    o, p = _num(q.open), _num(q.last_price)
    if o is None or p is None:
        return hi, lo
    # 夜盘：当天的高低价还没换到本场，判断不了哪个值属于本场，只取能确定包含本场的并集。
    # 高 = max(当日高, 本场开, 本场现)，低 = min(当日低, 本场开, 本场现)：
    # 低开时高值仍会带白天的旧高，是偏保守的包络；精确值要到交易所换日（约 22:45）后才有。
    if hi is None or lo is None:
        return max(o, p), min(o, p)
    return max(hi, o, p), min(lo, o, p)


def _snapshot(quotes, title: str) -> str:
    lines = [title, ""]
    for sym, q in quotes.items():
        p = q.last_price
        if math.isnan(p):
            lines.append(f"{sym}  暂无行情")
            continue
        pre = q.pre_close
        chg = f"{(p / pre - 1) * 100:+.2f}%" if pre and not math.isnan(pre) else "--"
        hi, lo = _session_ohlc(q, _session_state(sym, datetime.now()).get("label"))
        lines.append(f"{sym}  现价 {p}  开 {_num(q.open)}  高 {hi}  低 {lo}  {chg}")
    return "\n".join(lines)


def _sessions_for(sym: str):
    """拿品种对应的交易时段：先按完整代码匹配（CZCE.MA），再按去掉交易所前缀的代码（MA）匹配"""
    full = sym.replace("KQ.m@", "").replace("KQ.i@", "")
    segs = full.split(".")
    return config.SESSIONS.get(full) or (config.SESSIONS.get(segs[-1]) if segs else None) or []


def _session_state(sym: str, now) -> dict:
    """当前钟点是否落在该品种的某个交易时段里，以及是哪个时段。

    时段可跨零点（21:00→01:00），所以结束时间小于开始时间就算跨日。
    """
    hm = now.hour * 60 + now.minute
    for s in _sessions_for(sym):
        if len(s) != 2:
            continue
        sh, sm = (int(x) for x in s[0].split(":"))
        eh, em = (int(x) for x in s[1].split(":"))
        start, end = sh * 60 + sm, eh * 60 + em
        if start <= end:
            if start <= hm < end:
                return {"idx": s[0], "label": f"{s[0]}-{s[1]}"}
        elif hm >= start or hm < end:       # 跨零点，如夜盘 21:00-01:00
            return {"idx": s[0], "label": f"{s[0]}-{s[1]}"}
    return {"idx": None, "label": None}


def _levels_for(sym: str) -> list:
    """该品种的固定价位预警表，支持两种写法：
    ALERT_LEVELS = [3400, 3500]                        所有品种共用
    ALERT_LEVELS = {"KQ.m@CZCE.MA": [3500]}            按品种配
    """
    lv = config.ALERT_LEVELS
    if isinstance(lv, dict):
        lv = lv.get(sym, [])
    return [float(x) for x in (lv or [])]


def _key_day(now, cross: bool) -> str:
    """预警去重的「交易日」标记。

    夜盘跨零点后仍算前一天的交易时段，否则「每天晚上 9 点刚开盘」都会重新触发一遍。
    时钟不倒退（夏令时/手动校时）这个前提不成立时最多多推一次，不影响安全。
    """
    d = now.date()
    if cross and now.hour < 12:            # 凌晨 0-12 点归到前一天
        d = d - timedelta(days=1)
    return d.isoformat()


def _new_alert_state() -> dict:
    return {"levels": {}, "level_days": {}, "pre_close": None, "pct_fired": False}


def _check_alerts(sym: str, q, prev, st: dict, day: str) -> list:
    """价格预警。返回要推送的消息列表，并就地更新状态。

    - 固定价位：双向穿越才报（上/下），每个价位每天最多一次
    - 涨跌幅：|当日涨跌| 达到阈值才报，每天最多一次
    首次看到某品种时只播种状态不报——避免重启后拿着旧价当穿越。
    """
    if q.last_price is None or math.isnan(q.last_price):
        return []
    p, msgs = q.last_price, []
    first = sym not in prev

    # 固定价位
    now_above = {v: p >= v for v in _levels_for(sym)}
    if first:
        st["levels"] = now_above
    else:
        for v, above in now_above.items():
            was = st["levels"].get(v)
            if was is None:
                st["levels"][v] = above
                continue
            if above == was:
                continue
            st["levels"][v] = above
            if st["level_days"].get(v) == day:
                continue
            st["level_days"][v] = day
            msgs.append(f"【价格预警】{sym} 现价 {p} {'上穿' if above else '下破'} {v:g}")

    # 当日涨跌幅：用交易所预收价比，跨日/换月时归零不误报
    pre = getattr(q, "pre_close", None)
    if config.ALERT_PCT and pre and not math.isnan(pre):
        ref = st["pre_close"]
        if ref is not None and abs(pre - ref) > max(abs(ref) * 0.02, 5):
            st["pct_fired"] = False        # 昨收大幅跳变 → 换月或隔夜跳空，重新计
        st["pre_close"] = pre
        chg = (p / pre - 1) * 100
        if not first and abs(chg) >= config.ALERT_PCT and not st["pct_fired"]:
            st["pct_fired"] = True
            msgs.append(f"【涨跌幅预警】{sym} 现价 {p} 当日 {chg:+.2f}%"
                        f"（阈值 ±{config.ALERT_PCT:g}%）")
    return msgs


def _heartbeat():
    """独立线程：每天推一次心跳，周末节假日也推。
    心跳突然断了 = 程序或电脑挂了，你应该知道。"""
    hb_date = None
    while True:
        now = datetime.now()
        if now.hour >= config.HEARTBEAT_HOUR and hb_date != now.date():
            push.push("【心跳】系统在线")
            hb_date = now.date()
        time.sleep(60)


def market_test():
    """立即抓一次快照并推送，验证行情链路（非交易时段显示最近一次行情）"""
    api = _connect()
    if api is None:
        return
    try:
        quotes = {s: api.get_quote(s) for s in config.WATCH}
        try:
            api.wait_update(deadline=15)   # 盘后无更新会阻塞，15 秒兜底
        except Exception:
            pass                            # 超时也照推，用最后一次行情
        push.push(_snapshot(quotes, "【MVP测试】行情链路已打通，当前快照："))
    finally:
        api.close()


def _tick(sym, q, st, now):
    """处理一个品种的一个 tick，返回要推送的消息列表。

    快照的触发时机：
      - 离开某时段后，该品种在下个时段的第一个 tick → 补推上个时段的收盘快照
      - 进入时段的第一个 tick → 推开盘快照
    所以断线重连后回到同一时段不会重复推（那一段早就推过了）；
    盘后启动则等到下个时段开盘才有消息，不会凭空补一条「收盘」。
    """
    label = _session_state(sym, now).get("label")
    cross = bool((label or "").startswith(("21:", "22:", "23:", "00:")))
    key = f"{sym}@{label}" if label else None

    ast = st["alert"].setdefault(sym, _new_alert_state())
    msgs = _check_alerts(sym, q, st["prev"], ast, _key_day(now, cross))
    st["prev"][sym] = q.last_price

    if key is None:                      # 非交易时段：只更新预警状态，不动快照状态
        return msgs
    last = st["sst"].get(sym)
    if last == key:                      # 同一时段内的后续 tick，不重复推
        return msgs
    if last:                             # 跨时段：先补上个时段的收盘
        msgs.append(_snapshot({sym: q}, f"【{last.split('@', 1)[1]}】收盘快照"))
    msgs.append(_snapshot({sym: q}, f"【{label}】开盘快照"))
    st["sst"][sym] = key
    return msgs


def live():
    """常驻：心跳 + 各交易时段开盘/收盘快照 + 价格预警。策略逻辑以后加在内层循环里"""
    # 状态不跨重连重置：重连后没 tick 就不推，断线期间不会残留误报
    st = {"prev": {}, "alert": {}, "sst": {}}
    threading.Thread(target=_heartbeat, daemon=True).start()

    while True:  # 外层：连接级自愈
        api = _connect()
        if api is None:
            return  # 账号未配置，让外部守护决定下一步
        try:
            quotes = {s: api.get_quote(s) for s in config.WATCH}
            try:
                api.wait_update(deadline=15)   # 盘后无更新会阻塞，15 秒兜底
            except Exception:
                pass
            push.push(f"【上线】行情监控已启动，监控品种：{'、'.join(config.WATCH)}")

            while True:  # 内层：行情循环，异常抛给外层重连
                api.wait_update()  # 有行情更新才返回；非交易时段自动阻塞
                now = datetime.now()
                for sym, q in quotes.items():
                    if not q.datetime:      # 还没收到行情，别拿着 NaN 乱判断
                        continue
                    for msg in _tick(sym, q, st, now):
                        push.push(msg)
        except Exception as e:
            print(f"[market] 连接异常: {e}，10 秒后重连")
            time.sleep(10)
