#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QC 完成卡(云端) — 待检入库的下游。扫 入库登记台 入库状态=待检 且 QC卡发出时间空 ->
按出货批次聚合 -> 每行(产品/仓/到货/待检天数) + form(良品/不良/QC差异原因/备注) ->
聪哥3号发卡。仓库点【✅QC完成·上架】-> n8n inb_qc_done 回写 实际入库(良品)+不良+不明缺口
  +QC完成时间+QC用时(天)+差异原因(合并)+入库状态=已入库。发出后盖 QC卡发出时间 防重发。
🛡 dry-run(env INBOUND_QC_COMMIT=0)→发Frankie不盖时间; commit→发张灿煊(RECV_TARGET_UNION_ID)+盖时间。
"""
import sys, os, json, time, urllib.request, urllib.error
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
for _k in ("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"):
    os.environ.pop(_k, None)
import procurement_plan as pp
import procurement_inbound as pi

APP3_ID = os.environ.get("FEISHU_APP3_ID", ""); APP3_SECRET = os.environ.get("FEISHU_APP3_SECRET", "")
FRANKIE_UNION_ID = os.environ.get("FRANKIE_UNION_ID", "")
RECV_TARGET_UNION_ID = os.environ.get("RECV_TARGET_UNION_ID", "")
APP, RECV = pi.APP, pi.RECV
COMMIT = os.environ.get("INBOUND_QC_COMMIT", "0") == "1"
ONLY_BATCH = None
WH_EMOJI = {"国内自营仓":"🏠","海外仓":"🌍","跨境中转仓-美通":"🚢"}
QC_REASONS = ["无差异", "质量不良", "运输破损", "错发混发", "工厂漏发", "其他"]

def get_token3():
    req = urllib.request.Request("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": APP3_ID, "app_secret": APP3_SECRET}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())["tenant_access_token"]

def call3(tok, method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"https://open.feishu.cn/open-apis{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=utf-8"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode('utf-8','ignore')[:300]}")

def build_card(batch_no, rows):
    elements = [{"tag": "div", "text": {"tag": "lark_md",
        "content": "待检货 QC 完成后，请按各仓填 **良品入库数量**(进可用库存) 与 **不良品数量**(选填)，选 QC 差异原因。提交后系统登记上架并计 QC 用时。"}}]
    for r in rows:
        em = WH_EMOJI.get(r["whtype"], "📦")
        prod, sku = r.get("product") or "", r.get("sku") or ""
        if prod and sku:   pline = f"\n产品: **{prod}** (SKU: {sku})"
        elif sku:          pline = f"\nSKU: **{sku}**"
        elif prod:         pline = f"\n产品: **{prod}**"
        else:              pline = ""
        wd = r.get("wait_days")
        wtxt = f" · 待检 **{wd}** 天" if wd is not None else ""
        head = (f"{em} **{r['chan']}** · {r['whtype']}" + (f" · {r['wh_name']}" if r['wh_name'] else "")
                + pline + f"\n实际到货 **{r['arrived']:g}**" + wtxt)
        rid = r["record_id"]
        base = {"app_token": APP, "table_id": RECV, "record_id": rid, "arrived": r["arrived"], "chan": r["chan"]}
        elements += [
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": head}},
            {"tag": "form", "name": f"qf_{rid}", "elements": [
                {"tag": "input", "name": f"good_{rid}", "label_position": "left",
                 "label": {"tag": "plain_text", "content": "良品入库数量:"},
                 "placeholder": {"tag": "plain_text", "content": f"QC 合格进可用库存，默认 {r['arrived']:g}"}},
                {"tag": "input", "name": f"bad_{rid}", "label_position": "left",
                 "label": {"tag": "plain_text", "content": "不良品数量:"},
                 "placeholder": {"tag": "plain_text", "content": "选填；不填=到货-良品全算不良"}},
                {"tag": "multi_select_static", "name": f"rsn_{rid}",
                 "placeholder": {"tag": "plain_text", "content": "QC差异原因(可多选，无差异可不选)"},
                 "options": [{"text": {"tag": "plain_text", "content": x}, "value": x} for x in QC_REASONS]},
                {"tag": "input", "name": f"note_{rid}", "label_position": "left",
                 "label": {"tag": "plain_text", "content": "备注:"},
                 "placeholder": {"tag": "plain_text", "content": "QC说明/错发型号等(选填)"}},
                {"tag": "button", "action_type": "form_submit", "name": f"qcdone_{rid}",
                 "text": {"tag": "plain_text", "content": "✅QC完成·上架"}, "type": "primary",
                 "value": {**base, "action": "inb_qc_done"}},
            ]},
        ]
    elements += [{"tag": "hr"}, {"tag": "note", "elements": [{"tag": "plain_text",
        "content": "良品=QC合格进可用库存数。系统勾稽 不明缺口=到货-良品-不良，并计 QC 用时=完成-待检登记。上架后状态转『已入库』。"}]}]
    return {"config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": f"🔬 QC完成登记 · {batch_no}"}, "template": "blue"},
            "elements": elements}

def run():
    mode = "COMMIT(发张灿煊+盖发出时间)" if COMMIT else "DRY-RUN(发Frankie验渲染)"
    print(f"=== QC完成卡 [{mode}] ===")
    picks = {r["record_id"]: r["fields"] for r in pi.get_records(pi.PICK)}
    ships = {r["record_id"]: r["fields"] for r in pi.get_records(pi.SHIP)}
    mains = {r["record_id"]: r["fields"] for r in pi.get_records(pi.MAIN)}
    now = time.time()
    groups = {}
    for rec in pi.get_records(RECV):
        f = rec["fields"]
        if pi.sel(f.get("入库状态")) != "待检":
            continue
        if pi.num(f.get("QC卡发出时间")) > 0:
            continue
        pids = pi.link_ids(f.get("关联提货计划"))
        chan, batch_no, ship_prod = "?", "(未关联)", ""
        if pids and pids[0] in picks:
            pf = picks[pids[0]]
            chan = pi.sel(pf.get("渠道/站点")) or "?"
            sids = pi.link_ids(pf.get("关联出货批次"))
            if sids and sids[0] in ships:
                sf = ships[sids[0]]
                batch_no = pi.txt(sf.get("出货批次号")) or batch_no
                ship_prod = pi.txt(sf.get("本批SKU及数量"))
        sku = pi.txt(f.get("ERP SKU")); prod = ""
        mids = pi.link_ids(f.get("关联采购明细"))
        if mids and mids[0] in mains:
            prod = pi.txt(mains[mids[0]].get("产品名称"))
        if not prod: prod = ship_prod
        reg_ms = pi.num(f.get("待检登记时间"))
        wait_days = round((now - reg_ms/1000)/86400, 1) if reg_ms > 0 else None
        groups.setdefault(batch_no, []).append({
            "record_id": rec["record_id"], "chan": chan,
            "whtype": pi.sel(f.get("目的仓类型")) or "海外仓",
            "wh_name": pi.txt(f.get("仓库名")),
            "expect": pi.num(f.get("应入库数量")),
            "arrived": pi.num(f.get("实际到货数量")) or pi.num(f.get("应入库数量")),
            "sku": sku, "product": prod, "wait_days": wait_days})
    if not groups:
        print("  无『待检 且 未发QC卡』的行"); return
    tok3 = get_token3()
    now_ms = int(now * 1000)
    target = RECV_TARGET_UNION_ID if (COMMIT and RECV_TARGET_UNION_ID) else FRANKIE_UNION_ID
    for batch_no, rows in groups.items():
        if ONLY_BATCH and batch_no != ONLY_BATCH: continue
        card = build_card(batch_no, rows)
        resp = call3(tok3, "POST", "/im/v1/messages?receive_id_type=union_id",
            {"receive_id": target, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)})
        print(f"  批次 {batch_no}: {len(rows)}仓 -> {'真发张灿煊' if COMMIT else 'dry-run发Frankie'} message_id={resp.get('data',{}).get('message_id','?')}")
        if COMMIT:
            for r in rows:
                pi.put_fields(RECV, r["record_id"], {"QC卡发出时间": now_ms})

if __name__ == "__main__":
    run()
