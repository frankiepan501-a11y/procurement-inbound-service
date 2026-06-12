# -*- coding: utf-8 -*-
"""
B2 / Phase B-1 — 入库登记台「快捷入库·已入库」→ 领星 orderAdd 快捷入库(真录可用量库存)。
消除张灿煊"飞书入库 + 领星入库"双录。

仅同步: 入库状态=已入库 且 入库方式=快捷入库 且 领星同步状态∈(空/待同步)。
  (待检入库的货 = B-2 收货单流程, 本脚本不碰; QC完成的待检行入库方式=待检入库也不在此处。)

每行建领星采购入库单 orderAdd(type=2):
  sys_wid          系统仓库id ← 仓库映射表 tblBBOM07taRUtRQ「仓库ID(wid)」(实为系统仓库id, 同 WarehouseLists.wid)。
                   🚨 必须传 sys_wid(int) 而非 wid(自定义id,string)。旧版传 wid 报"自定义仓库ID没有对应系统仓库ID"=B2从没跑通的根因。
  sys_supplier_id  系统供应商id ← 出货台「供应商名称」匹配领星供应商表; 兜底=该 SKU 领星主供应商(supplier_quote is_primary)。
                   (orderAdd 即使传 order_sn 也不自动引供应商 → supplier 始终必填。)
  product_list[]   {sku=领星local_sku, good_num=实际入库数量(良品), bad_num=不良品数量, price=cg_price, seller_id=0, fnsku=""}
                   price=领星 productList.cg_price(采购单价); seller_id=0(无店铺,入本地仓)。
  order_sn         采购单号(快捷入库自动引单价×汇率)。⚠️ 仅当行内确有真实领星采购单号时用(env INBOUND_ERP_USE_ORDER_SN=1)。
                   飞书入库链当前与领星采购单解耦(B-2 的 3.1 工程才引入领星PO号+子项id), 故默认走显式 supplier+price 的 standalone 路径,
                   不猜 order_sn(防"采购单号不存在"报错)。
  inbound_idempotent_code = record_id(唯一; 防领星判重)
写回 入库台「领星入库单号」+「领星同步状态」=已同步。

🛡 默认 dry-run(只打印将录的 payload)。env INBOUND_ERP_COMMIT=1 或 ?commit=true 才真录。
铁律(高风险库存写): dry-run 核 payload → 1产品1仓 live(郭嘉美核对领星采购单/供应商/单价 + 张灿煊核对入库单, 错可 SetInboundOrderRevoke 撤销)→ 批量。绝不挂 poll。
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
# 默认不用 order_sn(飞书链与领星采购单解耦, 走显式 supplier+price)。B-2 引入真实领星PO号后可翻 1。
USE_ORDER_SN = os.environ.get("INBOUND_ERP_USE_ORDER_SN", "0") == "1"

# 口径(Frankie 2026-06-03 定稿): 采购入库只录"采购入库仓"; 下列字样仓=私人海外仓真实库存登记用, 不录采购入库。
NON_INBOUND_MARKERS = [m.strip() for m in os.environ.get(
    "ERP_NON_INBOUND_MARKERS", "海外仓,万邑通,波兰仓").split(",") if m.strip()]

def is_inbound_wh(name):
    """True=采购入库目的仓(本地仓/国内渠道仓/Temu全托/台湾凯发/千象国内中转仓等); False=库存登记仓不录。"""
    return not any(mk in (name or "") for mk in NON_INBOUND_MARKERS)

# ---------- sys_wid 映射(仓库映射表 = 领星系统仓库id 单一真相源) ----------
_WID_MAP = None
def build_wid_map():
    """仓库映射表 tblBBOM07taRUtRQ → {仓库名称: sys_wid}(与 procurement_to_erp 同源)。
    表里「仓库ID(wid)」存的就是领星系统仓库id(=WarehouseLists/local_inventory/warehouse 返回的 wid 字段, 文档标注'系统仓库id')。"""
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

def resolve_sys_wid(name):
    """仓库名 → 领星系统仓库id(精确; 不中去尾部括号后缀重试, 同 procurement_to_erp)。"""
    if not name:
        return None
    wm = build_wid_map()
    return wm.get(name) or wm.get(re.sub(r"[（(][^（()）]*[）)]\s*$", "", name).strip())

# ---------- 领星供应商表 {供应商名: sys_supplier_id} ----------
_SUP_MAP = None
def _norm(s):
    return re.sub(r"\s+", "", (s or "")).strip()

def build_supplier_map():
    global _SUP_MAP
    if _SUP_MAP is not None:
        return _SUP_MAP
    out = {}
    off = 0
    while True:
        r = pp.lx_api("/erp/sc/data/local_inventory/supplier", {"offset": off, "length": 200})
        rows = r.get("data") or []
        for s in rows:
            nm = _norm(s.get("supplier_name")); sid = s.get("supplier_id")
            if nm and sid:
                out[nm] = int(sid)
        if len(rows) < 200:
            break
        off += 200
    _SUP_MAP = out
    return out

# ---------- 领星 productList 元数据 {sku: {cg_price, supplier_id(主供应商)}} ----------
_SKU_META = None
def build_sku_meta():
    """全量扫 productList(sku 过滤参数失效→必须全量), 取 cg_price + 主供应商id(supplier_quote is_primary=1)。"""
    global _SKU_META
    if _SKU_META is not None:
        return _SKU_META
    out = {}
    off = 0
    while True:
        r = pp.lx_api("/erp/sc/routing/data/local_inventory/productList", {"offset": off, "length": 200})
        rows = r.get("data") or []
        for p in rows:
            sku = p.get("sku")
            if not sku:
                continue
            sup = None
            for q in (p.get("supplier_quote") or []):
                if q.get("is_primary") == 1:
                    sup = q.get("supplier_id"); break
            if sup is None:
                qs = p.get("supplier_quote") or []
                sup = qs[0].get("supplier_id") if qs else None
            out[str(sku)] = {"cg_price": pi.txt(p.get("cg_price")) or "", "supplier_id": int(sup) if sup else None}
        if len(rows) < 200:
            break
        off += 200
    _SKU_META = out
    return out

def resolve_supplier(ship_supplier_name, sku):
    """出货台供应商名 → 领星供应商id; 兜底=该 SKU 领星主供应商。"""
    if ship_supplier_name:
        sid = build_supplier_map().get(_norm(ship_supplier_name))
        if sid:
            return sid
    meta = build_sku_meta().get(sku) or {}
    return meta.get("supplier_id")

# ---------- 上下文反查 ----------
def _ship_supplier(recv_fields, picks, ships):
    """入库行 → 关联提货 → 关联出货批次 →「供应商名称」(出货台填)。"""
    for pid in pi.link_ids(recv_fields.get("关联提货计划")):
        pf = picks.get(pid)
        if not pf:
            continue
        for sid in pi.link_ids(pf.get("关联出货批次")):
            sf = ships.get(sid)
            if sf and pi.txt(sf.get("供应商名称")):
                return pi.txt(sf.get("供应商名称"))
    return ""

def _order_sn(recv_fields, picks, ships, contracts):
    """入库行 → 关联提货 → 出货批次 → 合同「合同编号」(=领星采购单号, 仅 USE_ORDER_SN 时用)。无真实关联→空(不猜)。"""
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
    return ""


def run():
    mode = "COMMIT(真录领星)" if COMMIT else "DRY-RUN(只打印payload)"
    print(f"=== B-1 入库(快捷入库)→领星 orderAdd [{mode}] order_sn={'on' if USE_ORDER_SN else 'off(显式supplier+price)'} ===")
    mains = {r["record_id"]: r["fields"] for r in pi.get_records(pi.MAIN)}
    picks = {r["record_id"]: r["fields"] for r in pi.get_records(PICK)}
    ships = {r["record_id"]: r["fields"] for r in pi.get_records(SHIP)}
    contracts = {r["record_id"]: r["fields"] for r in pi.get_records(CONTRACT)}
    n = done = skip = 0
    for rec in pi.get_records(RECV):
        f = rec["fields"]; rid = rec["record_id"]
        if pi.sel(f.get("入库状态")) != "已入库":
            continue
        if pi.sel(f.get("入库方式")) != "快捷入库":
            continue  # 待检入库的货走 B-2 收货单流程, 本脚本只录快捷入库
        syncst = pi.sel(f.get("领星同步状态"))
        if syncst not in ("", "待同步"):
            continue  # 已同步/N-A 跳过(幂等)
        n += 1
        ino = pi.txt(f.get("入库登记号"))
        sku = pi.txt(f.get("ERP SKU"))
        good = pi.num(f.get("实际入库数量")) or pi.num(f.get("实际到货数量"))
        bad = pi.num(f.get("不良品数量")) or 0
        # sys_wid: 仓库映射表(领星系统仓库id)。本地仓库名 = 入库行「仓库名」优先 → 退 关联采购明细→主表「本地仓库名」。
        wh = pi.txt(f.get("仓库名"))
        if not resolve_sys_wid(wh):
            for mid in pi.link_ids(f.get("关联采购明细")):
                mwh = pi.txt(mains.get(mid, {}).get("本地仓库名"))
                if resolve_sys_wid(mwh):
                    wh = mwh; break
        sys_wid = resolve_sys_wid(wh)
        if not sys_wid:
            print(f"  ⚠ 跳过 {ino}: 仓库名『{wh}』不在仓库映射表(请仓库填规范仓库名/或单表订单补本地仓库名)"); skip += 1; continue
        # 口径闸: 海外仓/万邑通/波兰仓 = 私人海外仓库存登记用, 采购入库不录。
        if not is_inbound_wh(wh):
            print(f"  ⚠ 跳过 {ino}: 仓库名『{wh}』是库存登记仓(海外仓/万邑通/波兰仓), 采购入库不录→请改采购入库仓"); skip += 1; continue
        if not sku or good <= 0:
            print(f"  ⚠ 跳过 {ino}: sku/数量缺({sku}/{good})"); skip += 1; continue
        # 供应商(必填) + 单价
        ship_sup = _ship_supplier(f, picks, ships)
        sys_supplier_id = resolve_supplier(ship_sup, sku)
        if not sys_supplier_id:
            print(f"  ⚠ 跳过 {ino}: 供应商解析失败(出货台供应商名『{ship_sup}』未匹配领星供应商表, 该SKU也无主供应商报价)"); skip += 1; continue
        meta = build_sku_meta().get(sku) or {}
        cg_price = meta.get("cg_price") or ""
        order_sn = _order_sn(f, picks, ships, contracts) if USE_ORDER_SN else ""
        item = {"sku": sku, "good_num": int(good), "bad_num": int(bad), "seller_id": 0, "fnsku": ""}
        payload = {
            "type": 2,                         # 2=采购入库
            "sys_wid": sys_wid,                # 🚨 系统仓库id(不是 wid)
            "sys_supplier_id": sys_supplier_id,
            "inbound_idempotent_code": rid,    # 唯一幂等键
            "product_list": [item],
        }
        if order_sn:
            payload["order_sn"] = order_sn     # 快捷入库自动引单价×汇率(此时不显式传 price)
        else:
            item["price"] = cg_price           # standalone: 显式单价 cg_price
        print(f"  入库 {ino} | 仓库『{wh}』→sys_wid{sys_wid} | sku{sku} 良{int(good)}/次{int(bad)} | 供应商id{sys_supplier_id}『{ship_sup or '(SKU主供应商)'}』 | {'order_sn='+order_sn if order_sn else 'price='+(cg_price or '?')}")
        print(f"    payload={json.dumps(payload, ensure_ascii=False)}")
        if not COMMIT:
            continue
        r = pp.lx_api(ORDERADD, payload)
        if r.get("code") in (0, "0", 200):
            data = r.get("data") or {}
            ib = data.get("order_sn") or (data.get("order_sn_arr") or [""])[0] or ""
            pp.feishu_api("PUT", f"{BASE}/tables/{RECV}/records/{rid}", {"fields": {"领星入库单号": str(ib)}})
            pp.feishu_api("PUT", f"{BASE}/tables/{RECV}/records/{rid}", {"fields": {"领星同步状态": "已同步"}})
            print(f"    ✓ 领星入库单 {ib}"); done += 1
        else:
            print(f"    ✗ orderAdd 失败 code={r.get('code')} msg={str(r.get('message'))[:180]}")
    print(f"=== B-1 完成 [{mode}] 候选{n} 真录{done} 跳过{skip} ===")
    return {"candidate": n, "synced": done, "skipped": skip}
