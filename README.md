# 期货信号 MVP

个人量化交易的最小可用系统：**行情链路 + 资讯链路 + 手机推送**，暂无任何策略逻辑。
程序只负责监控和推送，下单永远人工执行（不触发程序化交易报备）。

## 项目结构

```
config.py    所有配置（品种、关键词、账号）
push.py      企业微信群机器人推送
db.py        URL 去重（sqlite）
news.py      资讯：新浪滚动新闻 → 关键词过滤 → DeepSeek 总结 → 推送
market.py    行情：TqSdk 订阅 → 开盘/收盘快照推送
main.py      入口
```

## 三个账号（各 5 分钟，全部免费）

| 用途 | 去哪注册 | 填到 config.py |
|---|---|---|
| 手机推送 | 企业微信 → 建一个只有自己的群 → 群机器人 → 添加 → 复制 webhook | `PUSH_WEBHOOK` |
| 资讯总结 | platform.deepseek.com 注册，充值几块钱，创建 API key | `LLM_API_KEY` |
| 实时行情 | shinnytech.com 注册免费快期账号（无需实盘开户） | `TQ_USER` / `TQ_PASS` |

安装依赖：`pip install -r requirements.txt`

## 验证链路（各一条命令）

```bash
python main.py --market-test   # 手机应收到一条行情快照（周末/收盘后显示最近行情）
python main.py --news-once     # 手机应收到本轮命中的资讯总结
```

没有配置账号也能跑——会自动降级为本地打印，方便先看效果。

## 常驻运行

```bash
python main.py     # 行情开盘/收盘快照 + 资讯每 15 分钟一轮
```

在 Windows 上长期跑请配置电源为不休眠；后续建议迁移到轻量云服务器
（几十元/年），断网断电风险更低，夜盘时段也能覆盖。

## 下一步（MVP 之后） 有空会更新下一版

1. 加交易所公告源（上期所官网为 Vue 渲染，需调其 AJAX 接口；可先加转载源）
2. 加快讯源（财联社/金十）
3. 推送分级：公告秒推、快讯聚合、盘前汇总
4. 在 market.py 的 live() 循环里加策略信号逻辑
5. 部署到云服务器 + 进程守护
