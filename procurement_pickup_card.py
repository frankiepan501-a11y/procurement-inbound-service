#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B1.5 运营提货确认卡 — 发出货群 + @各渠道运营, 运营 form 填【最终提货确认数】。
读 提货计划台 分配状态=系统已算 的行 -> 按出货批次聚合 -> 每渠道一组(充足数据) ->
聪哥3号发交互卡。运营点 form 提交 -> n8n event-hub inb_* 分支回写 最终提货确认数 + 分配状态=运营已确认。

数据来源(每渠道行):
  渠道/运营(对照表) / 之前采购量(关联主表「采购确认量」, 无主表=standalone→"-") /
  本次提货需求(运营提货需求数量) / AI建议(AI建议分配数, 引擎已箱规取整) /
  本次总出货量(出货批次「本批出货数量」) / 毛利(主表「近30天毛利率」, 无→"-")

🛡 默认 dry-run: 卡片发 Frankie(union_id) 验渲染; --commit: 发出货群 chat_id + @运营。
铁律: 通知发真人前 dry-run→灰度; form input/button name 带 record_id 防 230099 duplicate。
用法: python procurement_pickup_card.py [--commit] [--batch 出货批次号]
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
SHIP_GROUP = os.environ.get("SHIP_GROUP", "oc_dbd9cc6bb5b19586a16a956fe2fd9ddb")  # ‼️备货及出货安排📦
APP, MAIN, PICK, SHIP = pi.APP, pi.MAIN, pi.PICK, pi.SHIP
# 云端: COMMIT/ONLY_BATCH/TEST_ALL 由 main.py 在调用 run() 前设模块全局
COMMIT = os.environ.get("INBOUND_CARDS_COMMIT", "0") == "1"
ONLY_BATCH = None
TEST_ALL = False

SITE_OP = {"美国":"黄奕纯","澳大利亚":"黄奕纯","加拿大":"陈翔宇","墨西哥":"陈翔宇","日本":"陈翔宇",
           "欧洲":"余培霓","英国":"余培霓","沃尔玛":"林明坚","美客多-墨西哥":"梁俊辉","美客多-巴西":"梁俊辉",
           "国内线上-淘宝正方体":"蔡宗佑","国内门店渠道-威":"马建威","B2B商务渠道仓-华":"冼浩华"}
SITE_EMOJI = {"美国":"🇺🇸","加拿大":"🇨🇦","墨西哥":"🇲🇽","欧洲":"🇪🇺","英国":"🇬🇧","日本":"🇯🇵",
              "澳大利亚":"🇦🇺","沃尔玛":"🛒","美客多-墨西哥":"🇲🇽","美客多-巴西":"🇧🇷",
              "国内线上-淘宝正方体":"🛍️","国内门店渠道-威":"🏬","B2B商务渠道仓-华":"🏢"}

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

def build_card(batch_no, ship_qty, sku, rows):
    """rows: list of dict(record_id, site, op_name, prev_buy, need, ai, margin)"""
    elements = [{"tag": "div", "text": {"tag": "lark_md",
        "content": f"本批出货 **{ship_qty:g}** 件（SKU: {sku}）。请各渠道运营核对后填【最终提货确认数】。\nAI 建议仅供参考，**以你填的为准**。"}}]
    for r in rows:
        em = SITE_EMOJI.get(r["site"], "•")
        head = (f"{em} **{r['site']}**（{r['op_name']}）\n"
                f"之前采购 {r['prev_buy']} · 本次需求 **{r['need']:g}** · AI建议 {r['ai']:g} · 毛利 {r['margin']}")
        base = {"app_token": APP, "table_id": PICK, "record_id": r["record_id"],
                "ai": r["ai"], "need": r["need"], "site": r["site"], "batch": batch_no}
        elements += [
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": head}},
            {"tag": "form", "name": f"f_{r['record_id']}", "elements": [
                {"tag": "input", "name": f"qty_{r['record_id']}", "label_position": "left",
                 "label": {"tag": "plain_text", "content": "最终提货确认数:"},
                 "placeholder": {"tag": "plain_text", "content": f"建议 {r['ai']:g}，填数字后点确认"}},
                {"tag": "button", "action_type": "form_submit", "name": f"submit_{r['record_id']}",
                 "text": {"tag": "plain_text", "content": "✅确认此渠道提货量"}, "type": "primary",
                 "value": {**base, "action": "inb_pick_confirm"}},
            ]},
        ]
    elements += [{"tag": "hr"}, {"tag": "note", "elements": [{"tag": "plain_text",
        "content": "确认后系统通知仓库按此数备货入库。出货量不够时，各渠道按实际协调。"}]}]
    return {"config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": f"📦 提货确认 · {batch_no}"}, "template": "blue"},
            "elements": elements}

def run():
    mode = "COMMIT(发出货群+@运营)" if COMMIT else "DRY-RUN(发Frankie验渲染)"
    print(f"=== 运营提货确认卡 [{mode}] ===")
    mains = {r["record_id"]: r["fields"] for r in pi.get_records(MAIN)}
    ships = {r["record_id"]: r["fields"] for r in pi.get_records(SHIP)}
    groups = {}
    for rec in pi.get_records(PICK):
        f = rec["fields"]
        st = pi.sel(f.get("分配状态"))
        if not TEST_ALL and st != "系统已算":
            continue
        sids = pi.link_ids(f.get("关联出货批次"))
        if not sids: continue
        groups.setdefault(sids[0], []).append(rec)
    if not groups:
        print("  无『系统已算』待运营确认的提货行" + ("（--test-all 可强制拉全部渲染）" if not TEST_ALL else "")); return
    tok3 = get_token3()
    for sid, recs in groups.items():
        sf = ships.get(sid, {})
        batch_no = pi.txt(sf.get("出货批次号")) or sid
        if ONLY_BATCH and batch_no != ONLY_BATCH: continue
        ship_qty = pi.num(sf.get("本批出货数量"))
        rows, sku = [], ""
        for rec in recs:
            f = rec["fields"]
            sku = pi.txt(f.get("ERP SKU")) or sku
            mids = pi.link_ids(f.get("关联采购明细"))
            prev_buy, margin = "-", "-"
            if mids and mids[0] in mains:
                mf = mains[mids[0]]
                prev_buy = f"{pi.num(mf.get('采购确认量')) or pi.num(mf.get('最终采购量')):g}"
                margin = pi.txt(mf.get("近30天毛利率")) or "-"
            rows.append({"record_id": rec["record_id"], "site": pi.sel(f.get("渠道/站点")),
                         "op_name": SITE_OP.get(pi.sel(f.get("渠道/站点")), "?"),
                         "prev_buy": prev_buy, "need": pi.num(f.get("运营提货需求数量")),
                         "ai": pi.num(f.get("AI建议分配数")), "margin": margin})
        card = build_card(batch_no, ship_qty, sku, rows)
        ops = sorted({r["op_name"] for r in rows if r["op_name"] != "?"})
        if COMMIT:
            print(f"  批次 {batch_no}: 发出货群, @{ops}  [真发]")
            resp = call3(tok3, "POST", "/im/v1/messages?receive_id_type=chat_id",
                {"receive_id": SHIP_GROUP, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)})
        else:
            print(f"  批次 {batch_no}: {len(rows)}渠道 应@{ops} → dry-run 发 Frankie 验渲染")
            resp = call3(tok3, "POST", "/im/v1/messages?receive_id_type=union_id",
                {"receive_id": FRANKIE_UNION_ID, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)})
        print(f"    message_id={resp.get('data',{}).get('message_id','?')}")

if __name__ == "__main__":
    run()
