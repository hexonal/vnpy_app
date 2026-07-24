# 分离式行情/交易路由(FUTU 喂行情 · uSMART 接单)

一个网关只喂行情、另一个网关只接单。`vnpy_router` 的 `RouterEngine` 把行情
订阅/历史 K 强制走**行情网关**,把下单强制走**交易网关**,并修掉 vnpy OMS
用 `vt_symbol` 单键存合约导致的冲突(FUTU 港股 `size=100` vs 交易网关同标的
`size=1` —— 谁后推谁覆盖)。

## 为什么需要它

vnpy 的 `MainEngine` 按 `gateway_name` 显式路由每一次 `subscribe` /
`send_order`,但 GUI 里用户不会每次都手选网关;而 `OmsEngine` 用 `vt_symbol`
单键存合约,两个网关推同一个港股标的时后者覆盖前者。RouterEngine 集中接管这
两件事:行情永远回 FUTU、订单永远去交易网关、合约表按角色分离。

## 启用(默认关闭 · fail-closed)

不建 `routing_setting.json` 时,`RouterEngine` 完全不安装任何 patch,终端行为
和单网关时一模一样。要启用:

1. 把样例拷到 vnpy 的配置目录 `~/.vntrader/`:

   ```bash
   cp routing_setting.json.example ~/.vntrader/routing_setting.json
   ```

2. 内容(两个字段都必填,且不能相同,否则 `RouterConfigError` 拒绝启动):

   ```json
   {
     "quote_gateway": "FUTU",
     "trade_gateway": "USMART"
   }
   ```

   `quote_gateway` / `trade_gateway` 的值必须是**已注册网关的名字**(GUI 首页
   "连接" 菜单里那些);拼错或没注册 → 启动时 `RouterConfigError` 拒绝(不装
   半套 patch)。

## PAPER vs LIVE(环境变量 `VNPY_ROUTING_PROFILE`)

| 档位 | 怎么开 | 下单落到哪 | PaperAccountApp |
|---|---|---|---|
| **PAPER**(默认) | 不设,或 `VNPY_ROUTING_PROFILE=PAPER` | 本地模拟撮合,**永不触及真实券商** | 加载 |
| **LIVE** | `VNPY_ROUTING_PROFILE=LIVE` | 真发到 `trade_gateway`(真钱) | **不加载** |

LIVE 档 `run_gui.py` 会**跳过** `PaperAccountApp` —— 它的 `send_order` 劫持会
无条件吞掉真实订单。`RouterEngine.verify_patch_chain("LIVE")` 是最后一道闸:
若 PaperAccountApp 在 LIVE 档仍被加载,它会在任何网关连接**之前**抛错,而不是
让真单被静默吞掉。

```bash
# 模拟盘(默认):FUTU 行情 + 本地撮合
.venv/bin/python run_gui.py

# 实盘分离路由:FUTU 行情 + uSMART 真下单
VNPY_ROUTING_PROFILE=LIVE .venv/bin/python run_gui.py
```

## uSMART 交易网关前置条件

`trade_gateway="USMART"` 要真能下单,连接 uSMART 时必须同时配 `trade_password`
和 `rsa_pub_key_path`(否则网关只读行情,`send_order` 直接 REJECTED)。上真钱
前先把 uSMART 连接里的 `environment` 设成 `"uat"`,在券商模拟 host 上验证下单
schema —— 交易 API 字段是照官方文档搭的,尚未用真实账户跑通(见
`vnpy_usmart/vnpy_usmart/trade_client.py` 顶部的 LIVE-CALIBRATION 说明)。

## 一句话记忆

- 不建配置文件 = 单网关,和以前一样。
- 建了 = FUTU 喂行情、USMART 接单,合约表分离。
- PAPER = 本地模拟;LIVE = 真钱,且自动不加载模拟盘 App。
