# -*- coding: utf-8 -*-
"""推送模块：企业微信群机器人；webhook 未配置时降级为本地打印"""
import requests
import config


def push(text: str) -> bool:
    """推送到手机。返回是否成功（未配置 webhook 时打印并视为成功）。"""
    text = text.strip()
    if not text:
        return False
    if not config.PUSH_WEBHOOK:
        print(f"[未配置webhook，本地打印] {text}")
        return True
    # 企业微信文本消息上限 2048 字节，超长截断
    payload = text.encode("utf-8")[:2048].decode("utf-8", errors="ignore")
    try:
        r = requests.post(
            config.PUSH_WEBHOOK,
            json={"msgtype": "text", "text": {"content": payload}},
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        if body.get("errcode") != 0:
            print(f"[推送失败] {body}")
            return False
        print(f"[已推送] {text[:60]}")
        return True
    except Exception as e:
        print(f"[推送异常] {e}")
        return False
