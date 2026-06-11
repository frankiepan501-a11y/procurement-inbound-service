#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B1.5 仓库入库卡 — 发仓库主管, form 填各仓实收数量。
读 入库登记台 入库状态=待入库 的行 -> 按出货批次聚合 -> 每行(渠道/目的仓类型/应入) ->
聪哥3号发交互卡。仓库点 form 提交 -> n8n event-hub inb_recv_confirm(同 inb_ 分支)回写
  实际入库数量 + 入库状态=已入库 -> 引擎 roll-up 主表→已入库·库存可发 (有主表关联时)。

🛡 默认 dry-run: 发 Frankie 验渲染; --commit: 发仓库主管(职务实时查/启动期=张灿煊)。
用法: python procurement_inbound_card.py [--commit] [--batch 出货批次号]
"""
import sys, os, json, urllib.request, urllib.error
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
for _k in ("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"):
    os.environ.pop(_k, None)
import procurement_plan as pp  # 云端 shim(同目录)
import procurement_inbound as pi

APP3_ID = os.environ.get("FEISHU_APP3_ID", ""); APP3_SECRET = os.environ.get("FEISHU_APP3_SECRET", "")
FRANKIE_UNION_ID = os.environ.get("FRANKIE_UNION_ID", "")
# 仓库主管真发收件人(张灿煊)= 飞书 union_id(跨同开发者 app 通用, 从飞书通讯录查得, 非邮箱/非硬编码职务)。
# 空→fallback Frankie(dry-run 阶段)。聪哥3号 receive_id_type=union_id 直发。
RECV_TARGET_UNION_ID = os.environ.get("RECV_TARGET_UNION_ID", "")
APP, PICK, SHIP, RECV = pi.APP, pi.PICK, pi.SHIP, pi.RECV
# 云端: COMMIT/ONLY_BATCH 由 main.py 在调用 run() 前设模块全局
COMMIT = os.environ.get("INBOUND_CARDS_COMMIT", "0") == "1"
ONLY_BATCH = None
WH_EMOJI = {"国内自营仓":"🏠","海外仓":"🌍","跨境中转仓-美通":"🚢"}

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

REASONS = ["无差异", "工厂多发", "工厂少发", "工厂漏发", "错发混发", "其他"]

def build_card(batch_no, rows):
    """rows: list of dict(record_id, chan, whtype, wh_name, expect, sku, product)"""
    elements = [{"tag": "div", "text": {"tag": "lark_md",
        "content": "货到仓后, 请按各仓填 **实际到货数量** 与差异原因(仅数量类), 再选入库方式:\n**⚡快捷入库**=免检/抽检快, 直接进可用库存; **🔬待检入库**=需QC, 进待检待上架(QC结果稍后登记)。"}}]
    for r in rows:
        em = WH_EMOJI.get(r["whtype"], "📦")
        prod, sku = r.get("product") or "", r.get("sku") or ""
        if prod and sku:   pline = f"\n产品: **{prod}** (SKU: {sku})"
        elif sku:          pline = f"\nSKU: **{sku}**"
        elif prod:         pline = f"\n产品: **{prod}**"
        else:              pline = ""
        head = (f"{em} **{r['chan']}** · {r['whtype']}" + (f" · {r['wh_name']}" if r['wh_name'] else "")
                + pline + f"\n计划应到 **{r['expect']:g}**")
        rid = r["record_id"]
        base = {"app_token": APP, "table_id": RECV, "record_id": rid,
                "expect": r["expect"], "chan": r["chan"]}
        elements += [
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": head}},
            {"tag": "form", "name": f"f_{rid}", "elements": [
                {"tag": "input", "name": f"arr_{rid}", "label_position": "left",
                 "label": {"tag": "plain_text", "content": "实际到货数量:"},
                 "placeholder": {"tag": "plain_text", "content": f"整批实际到货，默认 {r['expect']:g}"}},
                {"tag": "multi_select_static", "name": f"rsn_{rid}",
                 "placeholder": {"tag": "plain_text", "content": "差异原因(可多选，无差异可不选)"},
                 "options": [{"text": {"tag": "plain_text", "content": x}, "value": x} for x in REASONS]},
                {"tag": "input", "name": f"note_{rid}", "label_position": "left",
                 "label": {"tag": "plain_text", "content": "备注:"},
                 "placeholder": {"tag": "plain_text", "content": "差异说明/错发型号等(选填)"}},
                {"tag": "button", "action_type": "form_submit", "name": f"quick_{rid}",
                 "text": {"tag": "plain_text", "content": "⚡快捷入库(免检/抽检快)"}, "type": "primary",
                 "value": {**base, "action": "inb_recv_quick"}},
                {"tag": "button", "action_type": "form_submit", "name": f"qc_{rid}",
                 "text": {"tag": "plain_text", "content": "🔬待检入库(需QC)"}, "type": "default",
                 "value": {**base, "action": "inb_recv_qc"}},
            ]},
        ]
    elements += [{"tag": "hr"}, {"tag": "note", "elements": [{"tag": "plain_text",
        "content": "差异原因只填数量类(多发/少发/漏发/错发)。质量不良/破损属QC, 待检入库后QC完成再登记。两入库方式请与领星前端操作一致。"}]}]
    return {"config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": f"📥 入库登记 · {batch_no}"}, "template": "green"},
            "elements": elements}

def run():
    mode = "COMMIT(发仓库主管)" if COMMIT else "DRY-RUN(发Frankie验渲染)"
    print(f"=== 仓库入库卡 [{mode}] ===")
    picks = {r["record_id"]: r["fields"] for r in pi.get_records(PICK)}
    ships = {r["record_id"]: r["fields"] for r in pi.get_records(SHIP)}
    mains = {r["record_id"]: r["fields"] for r in pi.get_records(pi.MAIN)}
    groups = {}
    for rec in pi.get_records(RECV):
        f = rec["fields"]
        if pi.sel(f.get("入库状态")) != "待入库":
            continue
        # 入库行 → 关联提货 → 渠道 + 出货批次
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
        sku = pi.txt(f.get("ERP SKU"))
        prod = ""
        mids = pi.link_ids(f.get("关联采购明细"))
        if mids and mids[0] in mains:
            prod = pi.txt(mains[mids[0]].get("产品名称"))
        if not prod:
            prod = ship_prod  # fallback: 出货台「本批SKU及数量」(standalone 无主表时)
        groups.setdefault(batch_no, []).append({
            "record_id": rec["record_id"], "chan": chan,
            "whtype": pi.sel(f.get("目的仓类型")) or "海外仓",
            "wh_name": pi.txt(f.get("仓库名")), "expect": pi.num(f.get("应入库数量")),
            "sku": sku, "product": prod})
    if not groups:
        print("  无『待入库』登记行(需先 procurement_inbound.py --commit 物化, 且提货=运营已确认)"); return
    tok3 = get_token3()
    for batch_no, rows in groups.items():
        if ONLY_BATCH and batch_no != ONLY_BATCH: continue
        card = build_card(batch_no, rows)
        if COMMIT:
            if RECV_TARGET_UNION_ID:
                print(f"  批次 {batch_no}: {len(rows)}仓 → 发仓库主管 张灿煊(union_id) [真发]")
                resp = call3(tok3, "POST", "/im/v1/messages?receive_id_type=union_id",
                    {"receive_id": RECV_TARGET_UNION_ID, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)})
            else:
                print(f"  批次 {batch_no}: {len(rows)}仓 → 仓库主管 union_id 未配, fallback 发 Frankie [真发]")
                resp = call3(tok3, "POST", "/im/v1/messages?receive_id_type=union_id",
                    {"receive_id": FRANKIE_UNION_ID, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)})
        else:
            print(f"  批次 {batch_no}: {len(rows)}仓 → dry-run 发 Frankie 验渲染")
            resp = call3(tok3, "POST", "/im/v1/messages?receive_id_type=union_id",
                {"receive_id": FRANKIE_UNION_ID, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)})
        print(f"    message_id={resp.get('data',{}).get('message_id','?')}")

if __name__ == "__main__":
    run()
