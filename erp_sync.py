# -*- coding: utf-8 -*-
"""
B2 — 入库登记 → 领星 orderAdd 快捷入库(真录库存)。
读 入库登记台 入库状态=已入库 且 领星同步状态∈(空/待同步) → 每行建领星采购入库单:
  order_sn = 采购单号(从 入库行→关联提货→关联出货批次→关联合同「合同编号」反查; 传它=快捷入库自动引用供应商/单价)
  product_list = [{sku=ERP SKU(领星local_sku), good_num=实际入库数量}]
  wid = 渠道/站点 → 领星本地仓(CHAN_TO_WID 草案, env LINGXING_WID_MAP 可覆盖)
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

# 渠道/站点 → 领星本地仓 wid(草案, 名称逐一对上领星 31 仓; Frankie/采购 确认后定稿)。
# env LINGXING_WID_MAP(JSON) 可覆盖, 无需改代码。
CHAN_TO_WID = {
    "美国": 5893, "加拿大": 5897, "墨西哥": 5896, "日本": 5895,
    "欧洲": 5894, "英国": 5898, "澳大利亚": 15278,
    "沃尔玛": 4929, "美客多-墨西哥": 4928, "美客多-巴西": 12357,
    "国内门店渠道-威": 14837, "国内线上-淘宝正方体": 14839,
}
try:
    CHAN_TO_WID.update(json.loads(os.environ.get("LINGXING_WID_MAP", "") or "{}"))
except Exception:
    pass

COMMIT = os.environ.get("INBOUND_ERP_COMMIT", "0") == "1"


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
        # wid: 优先 渠道/站点(经关联提货), 退 仓库名直配领星仓名
        chan = ""
        for pid in pi.link_ids(f.get("关联提货计划")):
            if pid in picks:
                chan = pi.sel(picks[pid].get("渠道/站点")) or chan
        wid = CHAN_TO_WID.get(chan)
        order_sn = _order_sn(f, picks, ships, contracts)
        if not wid:
            print(f"  ⚠ 跳过 {ino}: 渠道『{chan}』无 wid 映射(补 CHAN_TO_WID/env)"); skip += 1; continue
        if not sku or good <= 0:
            print(f"  ⚠ 跳过 {ino}: sku/数量缺({sku}/{good})"); skip += 1; continue
        payload = {
            "type": 2,                                  # 2=采购入库
            "wid": wid,
            "order_sn": order_sn,                        # 采购单号→快捷入库(自动引供应商/单价)
            "inbound_idempotent_code": rid,              # 唯一幂等键
            "product_list": [{"sku": sku, "good_num": int(good), "bad_num": 0}],
        }
        print(f"  入库 {ino} | 渠道{chan}→wid{wid} | sku{sku}×{int(good)} | order_sn={order_sn or '(空,需全product_list)'}")
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
