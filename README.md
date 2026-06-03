# procurement-inbound-service

采购到货入库工作流的云端触发服务（Zeabur, 项目 n8n-aments）。由 n8n cron 定时 HTTP 触发，让到货入库链 24/7 自跑、不依赖本地开机。

引擎/卡片移植自 Frankie 主端 `~/scripts/procurement_inbound.py` + `procurement_pickup_card.py` + `procurement_inbound_card.py`，**凭据全走 env**（public repo 零硬编）。领星走 n8n `lingxing-proxy`（出站 IP 白名单），飞书 bitable 用聪哥2号，卡片用聪哥3号。

## 端点（BEARER 鉴权；可 `?commit=true/false` 覆盖 env 默认）

| 端点 | 作用 | 默认 |
|---|---|---|
| `POST /inbound/sweep` | archive→stamp→alloc→materialize→rollup（飞书内状态推进） | env `INBOUND_COMMIT` |
| `POST /inbound/send-pickup-cards` `?batch=&test_all=` | 提货「系统已算」→ 运营卡（dry发Frankie / commit发出货群） | env `INBOUND_CARDS_COMMIT` |
| `POST /inbound/send-recv-cards` `?batch=` | 入库「待入库」→ 仓库卡（dry发Frankie / commit发张灿煊） | env `INBOUND_CARDS_COMMIT` |
| `POST /inbound/sla` | 生产跟催+出货/提货/入库超时 → 通知（dry发Frankie / commit发LOG群+L3抄送） | env `INBOUND_SLA_COMMIT` |
| `GET /health` / `GET /openapi.json` | 健康 / 部署 sentinel | — |

## 环境变量

| 变量 | 说明 |
|---|---|
| `BEARER` | 端点鉴权 token |
| `FEISHU_APP2_ID` / `FEISHU_APP2_SECRET` | 聪哥2号（bitable 读写主表+3附属表） |
| `FEISHU_APP3_ID` / `FEISHU_APP3_SECRET` | 聪哥3号（发卡片/SLA 通知） |
| `LINGXING_PROXY_URL` / `LINGXING_PROXY_TOKEN` | n8n 领星代理（箱规/生产周期 cg_box_pcs/cg_delivery） |
| `FRANKIE_UNION_ID` | 聪哥3号 ns，dry-run 卡片/SLA 收件人 |
| `INBOUND_COMMIT` | sweep 真写总闸（0=dry-run，默认 0） |
| `INBOUND_CARDS_COMMIT` | 卡片真发总闸（默认 0） |
| `INBOUND_SLA_COMMIT` | SLA 真发总闸（默认 0） |
| `SHIP_GROUP` / `LOG_GROUP` | 出货群 / LOG 业务群 chat_id（默认同一群 oc_dbd9…） |
| `RECV_TARGET_EMAIL` | 仓库主管（张灿煊）邮箱 → 聪哥3号 ns open_id；空则 fallback Frankie |

🛡 灰度：dry-run→单批→真发，绝不一上来群发。AI 不碰真实物流/签署；不写领星（B2 另做）。
