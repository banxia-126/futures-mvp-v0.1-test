# -*- coding: utf-8 -*-
"""资讯模块：新浪滚动新闻 → 去重 → 关键词过滤 → DeepSeek 总结 → 推送

MVP 只有新浪一个源；以后加交易所公告/快讯源时，在 fetch_sources() 里
追加解析函数即可，下游管线不变。

去重状态在 db.py：只有 LLM 给出定论（相关/无关）才停止重判，
失败和漏答的条目下轮还会再进一次 LLM。
"""
import json
import re
import time

import requests

import config
import db
import push

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}


def fetch_sina() -> list:
    """抓新浪滚动新闻最新条目，返回 [{title, url, source}]"""
    items = []
    for lid in config.SINA_LIDS:
        try:
            r = requests.get(
                "https://feed.mix.sina.com.cn/api/roll/get",
                params={"pageid": "153", "lid": lid,
                        "num": str(config.NEWS_NUM_PER_LID), "page": "1"},
                headers=HEADERS, timeout=15,
            )
            r.raise_for_status()
            for x in r.json()["result"]["data"]:
                items.append({
                    "title": x.get("title", ""),
                    "url": x.get("url", ""),
                    "source": f"新浪{lid}",
                })
        except Exception as e:
            print(f"[news] 新浪频道 {lid} 抓取失败: {e}")
    return items


def fetch_sources() -> list:
    """所有资讯源的统一入口。以后在这里追加新源。

    按 URL 去重：不同频道（如新浪 2516/2517）会返回重叠条目，
    不去重的话同一 URL 会在同一批出现两次，被判两遍、推两条。
    """
    seen, out = set(), []
    for it in fetch_sina():
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    return out


def fetch_body(url: str) -> str:
    """抓正文纯文本（失败返回空串，标题仍可用于判断）"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.encoding = "utf-8"
        text = re.sub(r"<[^>]+>", "", r.text)
        text = re.sub(r"\s+", " ", text)
        return text[:3000]
    except Exception:
        return ""


def filter_keywords(items: list) -> list:
    """标题命中品种关键词才进入下一环节，挡掉大部分噪音"""
    return [it for it in items
            if any(k in it["title"] for k in config.KEYWORDS)]


def llm_summarize(items: list):
    """DeepSeek 批量判断。成功返回 [{idx, relevant, direction, summary}]，失败返回 None"""
    lines = "\n".join(
        f"[{i+1}] 标题：{it['title']}\n正文：{it['text'][:800] or '（无正文）'}"
        for i, it in enumerate(items)
    )
    sys_prompt = (
        f"你是甲醇期货研究员，唯一任务：判断每条资讯对【{config.CORE_FOCUS}】价格的影响。\n"
        f"\n"
        f"判断标准只有一个——这条资讯会不会影响甲醇价格。\n"
        f"可能传导到甲醇的因素：{config.DRIVERS}。\n"
        f"\n"
        "规则：\n"
        "1. 其他品种的行情，只有能说清对甲醇的传导路径时才算相关"
        "（例：煤炭涨价→煤制甲醇成本抬升→利多；聚丙烯跌价→MTO 利润压缩→甲醇需求转弱→利空）。"
        "只是提到别的品种、或只讲该品种自身的涨跌，一律 relevant=false，"
        "哪怕它是铜、黄金、原油这类重要品种。\n"
        "2. summary 必须落到甲醇身上：写清通过什么路径影响（成本/供给/需求/进口/库存/情绪）、"
        "影响方向和关键数字。不要只复述其他品种的行情。\n"
        "3. direction 是「对甲醇」的方向：利多/利空/中性。\n"
        "4. 只基于给定资讯原文总结，严禁添加原文没有的信息；关键数字必须原样保留。\n"
        "5. 输出 JSON 对象：{\"items\":[{\"idx\":整数,"
        "\"relevant\":true/false,\"direction\":\"利多/利空/中性\","
        "\"summary\":\"一句话，说明对甲醇的影响路径与方向，含关键数字\"}]}\n"
        "6. idx 必须与输入条目的 [序号] 一致。"
    )
    for attempt in range(2):
        try:
            r = requests.post(
                f"{config.LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
                json={
                    "model": config.LLM_MODEL,
                    "temperature": 0.3,
                    "max_tokens": getattr(config, "LLM_MAX_TOKENS", 16000),
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user",
                         "content": f"逐条判断以下资讯对甲醇价格的影响：\n{lines}"},
                    ],
                    "response_format": {"type": "json_object"},
                },
                timeout=120,
            )
            if r.status_code != 200:
                # 不能只写 r.raise_for_status()：它把响应体整个丢掉，只剩一句
                # 没有信息量的 "400 Client Error"。模型名填错这类问题会被彻底
                # 吞掉，症状是「AI 静默失效、每轮都降级」，很难查。
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            choice = r.json()["choices"][0]
            content = (choice["message"].get("content") or "").strip()
            if not content:
                # 推理模型（deepseek-flash / deepseek-v4-pro）会先思考再写正文，
                # max_tokens 不够时 token 全烧在思考上、正文为空，
                # 且 finish_reason=length —— 看起来「调用成功但没有输出」。
                raise RuntimeError(
                    f"返回正文为空（finish_reason={choice.get('finish_reason')}）；"
                    f"若模型是推理型，请加大 LLM_MAX_TOKENS")
            return json.loads(content).get("items", [])
        except Exception as e:
            print(f"[news] LLM 调用失败（第{attempt+1}次）: {e}")
            time.sleep(2)
    return None


def run_cycle():
    """跑一轮：抓取 → 关键词过滤 → 是否待判 → 抓正文 → LLM 判断 → 推送 → 落定状态"""
    candidates = [it for it in filter_keywords(fetch_sources())
                  if it["url"] and db.is_new(it["url"])]
    # 抓正文前先截断，避免老条目积压时每轮发上百个 HTTP 请求
    candidates = candidates[:config.NEWS_BATCH_MAX * 2]
    for it in candidates:
        it["text"] = fetch_body(it["url"])
    print(f"[news] {len(candidates)} 条待判资讯")

    if not candidates:
        return
    items = candidates[:config.NEWS_BATCH_MAX]

    # 未配置 LLM key：降级为直接推标题（链路验证用）
    if not config.LLM_API_KEY:
        print("[news] 未配置 LLM_API_KEY，降级为推送原文标题")
        for it in items:
            if push.push(f"【资讯】{it['title']}\n{it['url']}"):
                db.mark_done(it["url"], "confirmed")
            else:
                db.mark_attempt(it["url"])
        return

    results = llm_summarize(items)
    if results is None:
        # 整批 LLM 调用失败（网络/限流/宕机）：这是「服务不可用」不是「这条判不了」，
        # 不计入条目的重试次数——否则一次半小时的故障就能把当轮所有资讯烧成 dropped。
        # 条目保持 pending，下轮原样重试；新闻本身会随时间自然失效。
        print("[news] LLM 调用失败，本轮跳过，下轮重试")
        return

    # 逐条落定：确认相关的标记并推送，明确无关的丢掉
    by_idx = {}
    for r in results:
        idx = r.get("idx", 0)
        if 1 <= idx <= len(items):
            by_idx[idx] = r
    for i, it in enumerate(items, 1):
        r = by_idx.get(i)
        if r is None:
            db.mark_attempt(it["url"])       # LLM 漏答这条，留着下轮再判
            continue
        if not r.get("relevant"):
            db.mark_done(it["url"], "dropped")
            continue
        ok = push.push(
            f"【甲醇·{r.get('direction', '')}】{r.get('summary', '')}\n"
            f"来源：{it['source']}｜{it['title']}\n{it['url']}"
        )
        if ok:
            db.mark_done(it["url"], "confirmed")
        else:
            # 推送失败（断网/限流）不能标终态，否则这条资讯静默丢失。
            # 保持可重试，attempts 上限和 6 小时过期兜底，不会无限循环。
            db.mark_attempt(it["url"])
