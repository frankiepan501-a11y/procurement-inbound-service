# -*- coding: utf-8 -*-
"""
采购到货入库工作流 — 云端触发服务 (FastAPI, 由 n8n cron HTTP 触发, 24/7 不依赖本地)。
引擎/卡片移植自 Frankie 主端 ~/scripts/procurement_inbound.py + 2 张卡片生成器, 凭据全走 env。
领星走 n8n lingxing-proxy(出站 IP 白名单), 飞书 bitable 用聪哥2号, 卡片用聪哥3号。

端点(BEARER 鉴权; 每个端点可 ?commit=true/false 覆盖 env 默认):
  POST /inbound/sweep              archive→stamp→alloc→materialize→rollup(飞书内状态推进)
  POST /inbound/send-pickup-cards  提货「系统已算」→ 运营卡(dry-run发Frankie / commit发出货群)
  POST /inbound/send-recv-cards    入库「待入库」→ 仓库卡(dry-run发Frankie / commit发张灿煊)
  POST /inbound/sla                生产跟催+出货/提货/入库超时 → 通知(dry-run发Frankie / commit发LOG群)
  GET  /health  GET /openapi.json(FastAPI 自带, 部署 sentinel)

🛡 默认全 dry-run(env INBOUND_COMMIT/INBOUND_CARDS_COMMIT/INBOUND_SLA_COMMIT 皆 0)。
铁律: 灰度 dry-run→单批→真发; 绝不一上来群发。
"""
import os, io, json, contextlib, urllib.request, urllib.error
from fastapi import FastAPI, Header, HTTPException

import procurement_plan as pp
import procurement_inbound as eng
import procurement_pickup_card as pcard
import procurement_inbound_card as rcard
import erp_sync

app = FastAPI(title="procurement-inbound")
E = os.environ.get
BEARER = E("BEARER", "")
APP3_ID = E("FEISHU_APP3_ID", "")
APP3_SECRET = E("FEISHU_APP3_SECRET", "")
FRANKIE_UNION_ID = E("FRANKIE_UNION_ID", "")
LOG_GROUP = E("LOG_GROUP", "oc_dbd9cc6bb5b19586a16a956fe2fd9ddb")  # LOG/INV 默认群 ‼️备货及出货安排📦
SLA_COMMIT_DEFAULT = E("INBOUND_SLA_COMMIT", "0") == "1"

FB = "https://open.feishu.cn/open-apis"


def _auth(authorization):
    if BEARER and authorization != "Bearer " + BEARER:
        raise HTTPException(status_code=401, detail="unauthorized")


def _capture(fn, *a, **kw):
    """跑引擎/卡片函数并捕获其 print 输出做可观测返回。"""
    buf = io.StringIO()
    err = None
    with contextlib.redirect_stdout(buf):
        try:
            ret = fn(*a, **kw)
        except Exception as e:
            err = repr(e); ret = None
    return buf.getvalue(), ret, err


def _qbool(v, default):
    if v is None:
        return default
    return str(v).lower() in ("1", "true", "yes", "y")


# ---------- 飞书发消息(聪哥3号; SLA 通知用) ----------
def _tok3():
    req = urllib.request.Request(FB + "/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": APP3_ID, "app_secret": APP3_SECRET}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=20).read())["tenant_access_token"]


def _send3(tok, receive_id_type, receive_id, msg_type, content):
    req = urllib.request.Request(
        f"{FB}/im/v1/messages?receive_id_type={receive_id_type}",
        data=json.dumps({"receive_id": receive_id, "msg_type": msg_type,
                         "content": json.dumps(content, ensure_ascii=False)}, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json; charset=utf-8"},
        method="POST")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        return {"code": e.code, "msg": e.read().decode("utf-8", "ignore")[:200]}


# 飞书通知统一规范: {emoji} [{biz}·{level}] {title}  (biz=LOG 头程/发货/采购)
_LEVEL = {"L3·异常看板(Frankie)": ("🔴", "P0"), "L2·部门负责人": ("🟠", "P1"), "L1·提醒": ("🟡", "P2")}


def _sla_card(alerts):
    """alerts: list[(level, what, who)] → 飞书富文本卡片。"""
    by = {}
    for lv, what, who in alerts:
        by.setdefault(lv, []).append((what, who))
    elements = []
    for lv in sorted(by, reverse=True):
        emoji, pl = _LEVEL.get(lv, ("🟡", "P2"))
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"**{emoji} [LOG·{pl}] {lv}**"}})
        for what, who in by[lv]:
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                "content": f"• {what}\n  → {who}"}})
        elements.append({"tag": "hr"})
    if elements and elements[-1]["tag"] == "hr":
        elements.pop()
    return {"config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "⏰ 到货入库 SLA 超时巡检"},
                       "template": "orange"},
            "elements": elements or [{"tag": "div", "text": {"tag": "lark_md", "content": "无超时项 ✅"}}]}


# ============================================================
@app.get("/health")
def health():
    return {"ok": True, "service": "procurement-inbound",
            "commit": {"sweep": eng.COMMIT, "cards": pcard.COMMIT, "sla": SLA_COMMIT_DEFAULT,
                       "erp": erp_sync.COMMIT}}


@app.post("/inbound/sweep")
def sweep(commit: bool = None, authorization: str = Header(default="")):
    _auth(authorization)
    eng.COMMIT = _qbool(commit, eng.COMMIT)
    # 只跑状态推进(不含 sla_check); 序列同引擎 run() 但去掉末尾 sla
    def _run():
        mode = "COMMIT" if eng.COMMIT else "DRY-RUN"
        print(f"=== 到货入库 sweep [{mode}] ===")
        eng.archive_contracts()
        eng.stamp_order()
        eng.alloc_pickup()
        eng.materialize_inbound()
        eng.rollup_main()
        print(f"=== sweep 完成 [{mode}] ===")
    out, _, err = _capture(_run)
    if err:
        raise HTTPException(status_code=500, detail={"error": err, "log": out[-2000:]})
    return {"ok": True, "commit": eng.COMMIT, "log": out}


@app.post("/inbound/send-pickup-cards")
def send_pickup_cards(commit: bool = None, batch: str = None, test_all: bool = False,
                      authorization: str = Header(default="")):
    _auth(authorization)
    pcard.COMMIT = _qbool(commit, pcard.COMMIT)
    pcard.ONLY_BATCH = batch
    pcard.TEST_ALL = bool(test_all)
    out, _, err = _capture(pcard.run)
    if err:
        raise HTTPException(status_code=500, detail={"error": err, "log": out[-2000:]})
    return {"ok": True, "commit": pcard.COMMIT, "batch": batch, "log": out}


@app.post("/inbound/send-recv-cards")
def send_recv_cards(commit: bool = None, batch: str = None,
                    authorization: str = Header(default="")):
    _auth(authorization)
    rcard.COMMIT = _qbool(commit, rcard.COMMIT)
    rcard.ONLY_BATCH = batch
    out, _, err = _capture(rcard.run)
    if err:
        raise HTTPException(status_code=500, detail={"error": err, "log": out[-2000:]})
    return {"ok": True, "commit": rcard.COMMIT, "batch": batch, "log": out}


@app.post("/inbound/sync-erp")
def sync_erp(commit: bool = None, authorization: str = Header(default="")):
    """B2 入库→领星 orderAdd 真录库存。默认 dry-run(只打印 payload)。高风险写, test-first。"""
    _auth(authorization)
    erp_sync.COMMIT = _qbool(commit, erp_sync.COMMIT)
    out, ret, err = _capture(erp_sync.run)
    if err:
        raise HTTPException(status_code=500, detail={"error": err, "log": out[-2000:]})
    return {"ok": True, "commit": erp_sync.COMMIT, "result": ret, "log": out}


@app.post("/inbound/sla")
def sla(commit: bool = None, authorization: str = Header(default="")):
    _auth(authorization)
    do_commit = _qbool(commit, SLA_COMMIT_DEFAULT)
    out, alerts, err = _capture(eng.sla_check)
    if err:
        raise HTTPException(status_code=500, detail={"error": err, "log": out[-2000:]})
    alerts = alerts or []
    sent = []
    if alerts:
        tok = _tok3()
        card = _sla_card(alerts)
        if do_commit:
            # biz=LOG → 备货及出货群; L3(P0) 额外抄送 Frankie
            r = _send3(tok, "chat_id", LOG_GROUP, "interactive", card)
            sent.append({"to": "LOG群", "message_id": r.get("data", {}).get("message_id"), "raw": r.get("code")})
            if any(lv.startswith("L3") for lv, _, _ in alerts) and FRANKIE_UNION_ID:
                r2 = _send3(tok, "union_id", FRANKIE_UNION_ID, "interactive", card)
                sent.append({"to": "Frankie(L3抄送)", "message_id": r2.get("data", {}).get("message_id")})
        else:
            r = _send3(tok, "union_id", FRANKIE_UNION_ID, "interactive", card)
            sent.append({"to": "Frankie(dry-run)", "message_id": r.get("data", {}).get("message_id"), "raw": r.get("code")})
    return {"ok": True, "commit": do_commit, "alert_count": len(alerts), "sent": sent, "log": out}
