# 期货信号 MVP

个人量化交易的最小可用系统：**行情链路 + 资讯链路 + 手机推送**，暂无任何策略逻辑。
程序只负责监控和推送，下单永远人工执行（不触发程序化交易报备）。

## 项目结构

```
config.py    所有配置（品种、关键词、账号）—— 由 config.example.py 复制而来，不进版本库
push.py      企业微信群机器人推送
db.py        URL 去重（sqlite）
news.py      资讯：新浪滚动新闻 → 关键词过滤 → DeepSeek 总结 → 推送
market.py    行情：TqSdk 订阅 → 开盘/收盘快照推送
main.py      入口
```

## 安装

```bash
pip install -r requirements.txt
cp config.example.py config.py    # config.py 含账号，已在 .gitignore 中，不会被提交
```

## 三个账号（各 5 分钟，全部免费）

填到上一步复制出来的 `config.py`：

| 用途 | 去哪注册 | 填到 config.py |
|---|---|---|
| 手机推送 | 企业微信 → 建一个只有自己的群 → 群机器人 → 添加 → 复制 webhook | `PUSH_WEBHOOK` |
| 资讯总结 | platform.deepseek.com 注册，充值几块钱，创建 API key | `LLM_API_KEY` |
| 实时行情 | shinnytech.com 注册免费快期账号（无需实盘开户） | `TQ_USER` / `TQ_PASS` |

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

## 保障常驻运行（三层自愈）

没有绝对"时时刻刻"，只有逐层兜底。当前配置（已自动设置好）：

| 故障 | 兜底 | 状态 |
|---|---|---|
| TqSdk 断线 | `market.py` 内 10 秒后自动重连 | ✅ 已内置 |
| 进程崩溃 | `start.bat` 无限循环拉起（崩溃→等10秒→重启） | ✅ 已创建 |
| 电脑重启 | 启动文件夹 `FuturesMVP.bat` 登录自动启动（最小化窗口） | ✅ 已配置 |
| 电脑睡眠/休眠 | 电源计划已改为永不睡眠/休眠（AC+DC） | ✅ 已设置 |
| 系统挂了但你不知道 | 每天 08:00 推【心跳】，**哪天没收到心跳 = 出事了** | ✅ 已内置 |

日常用法：

- 双击 `start.bat` 开始常驻（第一次手动启动，之后开机自动）
- 平时看不到程序是正常的——消息在手机上
- 删除自启：删掉启动文件夹里的 `FuturesMVP.bat`；恢复睡眠：控制面板电源选项

**残留风险与升级路径**：关机断电、无网络、电脑硬件故障仍无法自愈。
当你想覆盖夜盘、要真正的 7×24 时，迁移到轻量云服务器（新用户约 50 元/年），
用 systemd 守护（把下面文件保存为 `/etc/systemd/system/futures-mvp.service`）：

```ini
[Unit]
Description=Futures MVP
After=network-online.target

[Service]
WorkingDirectory=/root/futures-mvp
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

然后 `systemctl enable --now futures-mvp`。服务器上的效果与本机三层保障等价，
且不依赖你的电脑开机。

## 下一步（MVP 之后） 有空会更新下一版

1. 加交易所公告源（上期所官网为 Vue 渲染，需调其 AJAX 接口；可先加转载源）
2. 加快讯源（财联社/金十）
3. 推送分级：公告秒推、快讯聚合、盘前汇总
4. 在 market.py 的 live() 循环里加策略信号逻辑
5. 部署到云服务器 + 进程守护
