# -*- coding: utf-8 -*-
"""
B2 — 入库登记 → 领星 orderAdd 快捷入库(真录库存)。
读 入库登记台 入库状态=已入库 且 领星同步状态∈(空/待同步) → 每行建领星采购入库单:
  order_sn = 采购单号(从 入库行→关联提货→关联出货批次→关联合同「合同编号」反查; 传它=快捷入库自动引用供应商/单价)
  product_list = [{sku=ERP SKU(领星local_sku), good_num=实际入库数量}]
  wid = 仓库名 → 领星 wid(复用仓库映射表 tblBBOM07taRUtRQ 单一真相源, 同 procurement_to_erp)
  inbound_idempotent_code = record_id(唯一; 防领星判重)
写回 领星入库单号 + 领星同步状态=已同步。

🛡 默认 dry-run(只打印将录的 payload)。env INBOUND_ERP_COMMIT=1 或 ?commit=true 才真录。
铁律: 真录是高风险库存写 → 先 dry-run 核 payload → 1产品1仓 live 测(采购+仓库在领星核对+可 SetInboundOrderRevoke 撤销)→ 批量。绝不挂 poll。
⚠️ 2001006: product_list 是对象数组, 领星签名需 compact JSON。本服务领星走 n8n lingxing-proxy, 由 proxy 端签名;
   live 测若报 2001006, 需在 lingxing-proxy 的 makeSign 确认对象/数组用 compact(separators)序列化。
"""
import os, json, re
import procurement_plan as pp
import procurement_inbound as pi

APP, PICK, SHIP, RECV, CONTRACT = pi.APP, pi.PICK, pi.SHIP, pi.RECV, pi.CONTRACT
BASE = pi.BASE
ORDERADD = "/erp/sc/routing/storage/storage/orderAdd"

COMMIT = os.environ.get("INBOUND_ERP_COMMIT", "0") == "1"

# 口径(Frankie 2026-06-03 定稿): 采购入库只录"采购入库仓"; 下列字样仓=私人海外仓真实库存登记用, 不录采购入库。
# 含字样即拒: 海外仓 / 万邑通(美国第三方海外仓×10) / 波兰仓(千象盒子)。env ERP_NON_INBOUND_MARKERS(逗号分隔)可覆盖。
NON_INBOUND_MARKERS = [m.strip() for m in os.environ.get(
    "ERP_NON_INBOUND_MARKERS", "海外仓,万邑通,波兰仓").split(",") if m.strip()]

def is_inbound_wh(name):
    """True=采购入库目的仓(本地仓/国内渠道仓/Temu全托/台湾凯发/千象国内中转仓等); False=库存登记仓不录。"""
    return not any(mk in (name or "") for mk in NON_INBOUND_MARKERS)

_WID_MAP = None
def build_wid_map():
    """仓库映射表 tblBBOM07taRUtRQ → {仓库名称: wid}(与 procurement_to_erp 同源, 单一真相源)。"""
    global _WID_MAP
    if _WID_MAP is not None:
        return _WID_MAP
    out = {}
    r = pp.feishu_api("GET",
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{pp.WAREHOUSE_APP_TOKEN}/tables/{pp.WAREHOUSE_TABLE_ID}/records?page_size=200")
    for it in (r.get("data") or {}).get("items", []):
        f = it["fields"]
        nm = pi.txt(f.get("仓库名称")); wid = pi.txt(f.get("仓库ID(wid)"))
        if nm and wid:
            try:
                out[nm] = int(float(wid))
            except (TypeError, ValueError):
                pass
    _WID_MAP = out
    return out

def resolve_wid(name):
    """仓库名 → wid(精确; 不中去尾部括号后缀重试, 同 procurement_to_erp)。"""
    if not name:
        return None
    wm = build_wid_map()
    return wm.get(name) or wm.get(re.sub(r"[（(][^（()）]*[）)]\s*$", "", name).strip())


def _order_sn(recv_fields, picks, ships, contracts):
    """入库行 → 关联提货 → 关联出货批次 → 关联合同 → 合同编号(=采购单号, 快捷入库 order_sn)。"""
    for pid in pi.link_ids(recv_fields.get("关联提货计划")):
        pf = picks.get(pid)
        if not pf:
            continue
        for sid in pi.link_ids(pf.get("关联出货批次")):
            sf = ships.get(sid)
            if not sf:
                continue
            for cid in pi.link_ids(sf.get("关联合同")):
                cf = contracts.get(cid)
                if cf:
                    sn = pi.txt(cf.get("合同编号"))
                    if sn:
                        return sn
    # 兜底: 入库号前缀(去 -提-渠道/-IN 后缀) = 采购单号(standalone 合同关联留空时)。
    # ⚠️ live 测须验证该 order_sn 能在领星快捷入库; 不行则改全 product_list(cg_price+supplier)。
    ino = pi.txt(recv_fields.get("入库登记号"))
    return re.sub(r"-提.*$|-IN$", "", ino) if ino else ""


def run():
    mode = "COMMIT(真录领星)" if COMMIT else "DRY-RUN(只打印payload)"
    print(f"=== B2 入库→领星 orderAdd [{mode}] ===")
    mains = {r["record_id"]: r["fields"] for r in pi.get_records(pi.MAIN)}
    picks = {r["record_id"]: r["fields"] for r in pi.get_records(PICK)}
    ships = {r["record_id"]: r["fields"] for r in pi.get_records(SHIP)}
    contracts = {r["record_id"]: r["fields"] for r in pi.get_records(CONTRACT)}
    n = done = skip = 0
    for rec in pi.get_records(RECV):
        f = rec["fields"]; rid = rec["record_id"]
        if pi.sel(f.get("入库状态")) != "已入库":
            continue
        syncst = pi.sel(f.get("领星同步状态"))
        if syncst not in ("", "待同步"):
            continue  # 已同步/N-A 跳过(幂等)
        n += 1
        ino = pi.txt(f.get("入库登记号"))
        sku = pi.txt(f.get("ERP SKU"))
        good = pi.num(f.get("实际入库数量")) or pi.num(f.get("应入库数量"))
        # wid: 复用仓库映射表(单一真相源)。本地仓库名 = 入库行「仓库名」(仓库填) 优先;
        #       退 关联采购明细→主表「本地仓库名」(采购计划单表已备好)。
        wh = pi.txt(f.get("仓库名"))
        if not resolve_wid(wh):
            for mid in pi.link_ids(f.get("关联采购明细")):
                mwh = pi.txt(mains.get(mid, {}).get("本地仓库名"))
                if resolve_wid(mwh):
                    wh = mwh; break
        wid = resolve_wid(wh)
        order_sn = _order_sn(f, picks, ships, contracts)
        if not wid:
            print(f"  ⚠ 跳过 {ino}: 仓库名『{wh}』不在仓库映射表(请仓库填规范仓库名/或单表订单补本地仓库名)"); skip += 1; continue
        # 口径闸(Frankie 2026-06-03 定稿): 海外仓/万邑通/波兰仓 = 私人海外仓真实库存登记用, 采购入库不录此仓。
        if not is_inbound_wh(wh):
            print(f"  ⚠ 跳过 {ino}: 仓库名『{wh}』是库存登记仓(海外仓/万邑通/波兰仓), 采购入库不录→请改采购入库仓"); skip += 1; continue
        if not sku or good <= 0:
            print(f"  ⚠ 跳过 {ino}: sku/数量缺({sku}/{good})"); skip += 1; continue
        payload = {
            "type": 2,                                  # 2=采购入库
            "wid": wid,
            "order_sn": order_sn,                        # 采购单号→快捷入库(自动引供应商/单价)
            "inbound_idempotent_code": rid,              # 唯一幂等键
            "product_list": [{"sku": sku, "good_num": int(good), "bad_num": 0}],
        }
        print(f"  入库 {ino} | 仓库『{wh}』→wid{wid} | sku{sku}×{int(good)} | order_sn={order_sn or '(空,需全product_list)'}")
        print(f"    payload={json.dumps(payload, ensure_ascii=False)}")
        if not COMMIT:
            continue
        r = pp.lx_api(ORDERADD, payload)
        if r.get("code") in (0, "0", 200):
            ib = (r.get("data") or {}).get("inbound_sn") or (r.get("data") or {}).get("order_sn") or ""
            # B2 自己 COMMIT 分支内直接写(不走 pi.put_fields, 它看引擎 COMMIT); 单选独立 PUT
            pp.feishu_api("PUT", f"{BASE}/tables/{RECV}/records/{rid}", {"fields": {"领星入库单号": str(ib)}})
            pp.feishu_api("PUT", f"{BASE}/tables/{RECV}/records/{rid}", {"fields": {"领星同步状态": "已同步"}})
            print(f"    ✓ 领星入库单 {ib}"); done += 1
        else:
            print(f"    ✗ orderAdd 失败 code={r.get('code')} msg={str(r.get('message'))[:150]}")
    print(f"=== B2 完成 [{mode}] 候选{n} 真录{done} 跳过{skip} ===")
    return {"candidate": n, "synced": done, "skipped": skip}
