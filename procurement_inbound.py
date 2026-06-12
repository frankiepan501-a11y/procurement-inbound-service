#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采购到货入库工作流 B1 引擎(飞书内闭环, 无外部写)。接在 Step6 采购合同之后。
单表+附属表架构(见 memory/project_procurement_workflow.md「到货入库工作流」段):
  主表 采购计划管理台 tblyRp10kr2y3Ue1(阶段列 roll-up)
    ├ 合同台 tblOe04QYeyYnalN
    ├ 出货计划台 tblFZAZPaDXmCzHV
    ├ 提货计划台 tblvj3xMz5qrdeQL  (★Step4 分配)
    └ 入库登记台 tblpscOM21zOqvpM

三个动作(run 顺序):
  1) archive_contracts: 合同 已签回+扫描件已传 → 自动标 已归档(文件已在云盘, 归档=状态留痕)
  2) alloc_pickup    : 提货行 待分配 且 出货批次=已出货 → 按【紧急度优先(主表可售天数最少先满)】
                       算 AI建议分配数 草稿 → 分配状态=系统已算 (采购再确认填 采购确认应到货数量→采购已确认)
  3) rollup_main     : 每主表行按关联的 合同/提货/入库 状态 roll-up 主表「阶段」
                       (已签回完成→已下单待出货→已出货待提货→已提货待入库→已入库·库存可发)

🛡 默认 dry-run(只打印将改的)。--commit 才真写。绝不碰真实物流/签署; 不写领星(B2 另做)。
铁律: 单选字段独立 PUT(防多字段 PUT 清空单选); 顶部 pop 代理(Clash 拦 open.feishu.cn)。
用法: python procurement_inbound.py [--commit]
"""
import sys, os, time
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
for _k in ("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"):
    os.environ.pop(_k, None)
import procurement_plan as pp  # 云端 shim(同目录)

APP = "D5LYbA8PuapxH0shWvMcpcHMnjb"
MAIN = "tblyRp10kr2y3Ue1"
CONTRACT = "tblOe04QYeyYnalN"
SHIP = "tblFZAZPaDXmCzHV"
PICK = "tblvj3xMz5qrdeQL"
RECV = "tblpscOM21zOqvpM"
BASE = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP}"
# 云端: dry-run 总闸由 env 控制; main.py 可在调用前设 procurement_inbound.COMMIT 覆盖
COMMIT = os.environ.get("INBOUND_COMMIT", "0") == "1"

# 主表阶段(本工作流只动 已签回完成 及之后的到货阶段, 不碰更早阶段)
ST_SIGNED   = "已签回完成"
ST_ORDERED  = "已下单待出货"
ST_SHIPPED  = "已出货待提货"
ST_PICKED   = "已提货待入库"
ST_STOCKED  = "已入库·库存可发"
INBOUND_STAGES = {ST_SIGNED, ST_ORDERED, ST_SHIPPED, ST_PICKED, ST_STOCKED}

# ---------- 字段值提取 ----------
def txt(v):
    if v is None: return ""
    if isinstance(v, str): return v.strip()
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, str): out.append(x)
            elif isinstance(x, dict): out.append(x.get("text") or x.get("name") or "")
        return "".join(out).strip()
    if isinstance(v, dict): return (v.get("text") or v.get("name") or "").strip()
    return str(v).strip()

def num(v):
    try:
        if isinstance(v, list) and v: v = v[0]
        if isinstance(v, dict): v = v.get("value") or v.get("text")
        return float(v)
    except (TypeError, ValueError): return 0.0

def sel(v):  # 单选 → 选项名
    if isinstance(v, str): return v.strip()
    if isinstance(v, list) and v:
        x = v[0]; return (x.get("text") or x.get("name") or "") if isinstance(x, dict) else str(x)
    if isinstance(v, dict): return (v.get("text") or v.get("name") or "").strip()
    return ""

def has_attach(v):  # 附件字段非空
    return bool(v) and isinstance(v, list) and len(v) > 0

def link_ids(v):  # 关联字段 → [record_id] (兼容多种格式)
    if not v: return []
    if isinstance(v, dict):
        return v.get("link_record_ids") or v.get("linked_record_ids") or v.get("record_ids") or []
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, str): out.append(x)
            elif isinstance(x, dict):
                if x.get("record_ids"): out += x["record_ids"]
                elif x.get("record_id"): out.append(x["record_id"])
                elif x.get("id"): out.append(x["id"])
        return out
    return []

# ---------- 箱规(领星 productInfo.cg_box_pcs) ----------
_SKU2ID = None  # 领星 local_sku -> product id (懒加载缓存)

def _load_sku2id():
    global _SKU2ID
    if _SKU2ID is not None:
        return _SKU2ID
    _SKU2ID = {}
    off = 0
    while True:
        r = pp.lx_api("/erp/sc/routing/data/local_inventory/productList", {"offset": off, "length": 200})
        rows = r.get("data") or []
        for p in rows:
            if p.get("sku"):
                _SKU2ID[str(p["sku"])] = p.get("id")
        if len(rows) < 200:
            break
        off += 200
    return _SKU2ID

_PINFO = {}  # sku -> productInfo data 缓存(box_pcs + delivery_days 共用)
def _pinfo(sku):
    if not sku: return {}
    if sku in _PINFO: return _PINFO[sku]
    pid = _load_sku2id().get(sku)
    d = {}
    if pid:
        try:
            d = pp.lx_api("/erp/sc/routing/data/local_inventory/productInfo", {"id": pid}).get("data") or {}
        except Exception:
            d = {}
    _PINFO[sku] = d
    return d

def _pos_int(v):
    try:
        return int(v) if v and int(v) > 0 else None
    except (TypeError, ValueError):
        return None

def box_pcs(sku):
    """领星 productInfo.cg_box_pcs(整箱pcs)。sku 须是领星 local_sku; 解析不到→None。"""
    return _pos_int(_pinfo(sku).get("cg_box_pcs"))

def delivery_days(sku):
    """领星 productInfo.cg_delivery(采购/生产周期天数)。解析不到→None。"""
    return _pos_int(_pinfo(sku).get("cg_delivery"))

def round_box(qty, box):
    """就近取整箱; box 缺失→原数。"""
    if not box or box <= 0: return round(qty)
    return int(round(qty / box) * box)

# ---------- 读写 ----------
def get_records(table):
    out, pt = [], None
    while True:
        url = f"{BASE}/tables/{table}/records?page_size=500" + (f"&page_token={pt}" if pt else "")
        r = pp.feishu_api("GET", url)
        if r.get("code") != 0:
            print(f"  ✗ 读 {table} 失败 {str(r)[:150]}"); break
        d = r.get("data") or {}
        out += d.get("items") or []
        if d.get("has_more") and d.get("page_token"):
            pt = d["page_token"]
        else:
            break
    return out

def put_fields(table, rid, fields):
    if not COMMIT:
        print(f"      [dry-run] PUT {table} {rid} {fields}"); return True
    r = pp.feishu_api("PUT", f"{BASE}/tables/{table}/records/{rid}", {"fields": fields})
    ok = r.get("code") == 0
    if not ok: print(f"      ✗ PUT 失败 {str(r)[:150]}")
    return ok

# ---------- 1. 合同归档 ----------
def archive_contracts():
    print("\n[1] 合同归档(已签回+扫描件 → 已归档)")
    n = 0
    for rec in get_records(CONTRACT):
        f = rec["fields"]; rid = rec["record_id"]
        status = sel(f.get("合同状态"))
        if status == "已签回" and has_attach(f.get("签回扫描件")):
            print(f"  合同 {txt(f.get('合同编号'))}: 已签回+扫描件 → 已归档")
            if put_fields(CONTRACT, rid, {"合同状态": "已归档"}): n += 1
    print(f"  归档 {n} 份" + ("" if COMMIT else " [dry-run]"))

# ---------- 1.5 下单盖戳 + 生产周期补全(合同=已下单生产) ----------
def stamp_order(now_ms=None):
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    print("\n[1.5] 下单时间盖戳 + 生产周期补全(合同=已下单生产)")
    mains = {r["record_id"]: r["fields"] for r in get_records(MAIN)}
    n = 0
    for rec in get_records(CONTRACT):
        f = rec["fields"]; rid = rec["record_id"]
        if sel(f.get("合同状态")) != "已下单生产":
            continue
        upd = {}
        if not f.get("下单时间"):
            upd["下单时间"] = now_ms
        if not num(f.get("生产周期天数")):
            ds = [delivery_days(txt(mains.get(mid, {}).get("ERP SKU")))
                  for mid in link_ids(f.get("关联采购明细"))]
            ds = [d for d in ds if d]
            if ds:
                upd["生产周期天数"] = max(ds)   # 多产品合同取最长生产周期
        if upd:
            print(f"  合同 {txt(f.get('合同编号'))}: 盖 {upd}")
            if put_fields(CONTRACT, rid, upd):
                n += 1
    print(f"  盖戳 {n} 份" + ("" if COMMIT else " [dry-run]"))

# ---------- 2. 提货分配草稿(紧急度优先) ----------
def alloc_pickup():
    print("\n[2] 提货分配草稿(待分配 + 出货已出货 → 紧急度优先)")
    mains = {r["record_id"]: r["fields"] for r in get_records(MAIN)}
    ships = {r["record_id"]: r["fields"] for r in get_records(SHIP)}
    picks = get_records(PICK)
    # 按出货批次分组待分配的提货行
    groups = {}
    for rec in picks:
        f = rec["fields"]
        if sel(f.get("分配状态")) != "待分配":
            continue
        sids = link_ids(f.get("关联出货批次"))
        if not sids:
            print(f"  ⚠ 提货行 {txt(f.get('提货计划号'))} 未关联出货批次, 跳过"); continue
        groups.setdefault(sids[0], []).append(rec)
    if not groups:
        print("  无待分配提货行" + ("" if COMMIT else " [dry-run]")); return
    for sid, rows in groups.items():
        sf = ships.get(sid, {})
        if sel(sf.get("出货状态")) != "已出货":
            print(f"  出货批次 {txt(sf.get('出货批次号'))} 未出货, 暂不分配该组"); continue
        avail = num(sf.get("本批出货数量"))
        # 每行需求 + 紧急度(主表可售天数, 越小越急; 缺失=很大=不急)
        items = []
        for rec in rows:
            f = rec["fields"]
            mids = link_ids(f.get("关联采购明细"))
            days = 9e9
            if mids and mids[0] in mains:
                d = num(mains[mids[0]].get("可售天数"))
                days = d if d > 0 else 9e9
            items.append({"rec": rec, "need": num(f.get("运营提货需求数量")), "days": days,
                          "name": txt(f.get("提货计划号")), "chan": sel(f.get("渠道/站点")),
                          "sku": txt(f.get("ERP SKU"))})
        total_need = sum(i["need"] for i in items)
        bn = txt(sf.get("出货批次号"))
        if total_need <= 0:
            print(f"  批次 {bn}: 提货行需求合计=0, 跳过(等运营填需求)"); continue
        if avail >= total_need:
            for i in items: i["alloc"] = i["need"]
            mode = f"满配(出货{avail:.0f}≥需求{total_need:.0f})"
        elif all(i["days"] >= 9e8 for i in items):
            # 无紧急度信号(旧订单/无主表关联): 退为按需求等比缩减
            for i in items: i["alloc"] = round(avail * i["need"] / total_need)
            mode = f"缺口·按需求等比(无可售天数, 出货{avail:.0f}<需求{total_need:.0f})"
        else:
            # 紧急度优先: 可售天数升序, 依次满足需求直到出货量用尽
            rem = avail
            for i in sorted(items, key=lambda x: x["days"]):
                give = min(i["need"], rem); i["alloc"] = give; rem -= give
            mode = f"缺口·紧急度优先(出货{avail:.0f}<需求{total_need:.0f})"
        # 箱规就近取整(单产品/批, 同 SKU): AI建议=整箱倍数; 解析不到领星sku→不取整(graceful)
        box = box_pcs(items[0]["sku"]) if items else None
        if box:
            for i in items: i["alloc"] = round_box(i["alloc"], box)
            mode += f" | 箱规{box}就近取整"
        print(f"  批次 {bn}: {mode}")
        for i in items:
            print(f"    {i['chan']}({i['name']}) 需求{i['need']:.0f} 可售{('%.0f'%i['days']) if i['days']<9e8 else '∞'}天 → AI建议{i['alloc']:.0f}")
            put_fields(PICK, i["rec"]["record_id"], {"AI建议分配数": i["alloc"]})
            put_fields(PICK, i["rec"]["record_id"], {"分配状态": "系统已算"})  # 单选独立 PUT

# ---------- 2.5 物化入库登记行(提货运营已确认 → 建待入库行) ----------
def materialize_inbound():
    print("\n[2.5] 物化入库登记行(提货=运营已确认 且 无入库行 → 建待入库)")
    picks = get_records(PICK)
    linked = set()
    for r in get_records(RECV):
        for pid in link_ids(r["fields"].get("关联提货计划")):
            linked.add(pid)
    n = 0
    for rec in picks:
        f = rec["fields"]; rid = rec["record_id"]
        if sel(f.get("分配状态")) != "运营已确认" or rid in linked:
            continue
        chan = sel(f.get("渠道/站点"))
        whtype = "国内自营仓" if chan.startswith("国内") else "海外仓"  # 跨境中转-美通 由仓库在卡上改
        qty = num(f.get("最终提货确认数"))
        pno = txt(f.get("提货计划号"))
        print(f"  建入库行: {pno}-IN  仓型={whtype} 应入={qty:.0f}")
        if not COMMIT:
            continue
        fields = {"入库登记号": f"{pno}-IN", "ERP SKU": txt(f.get("ERP SKU")),
                  "应入库数量": qty, "关联提货计划": [rid]}
        mids = link_ids(f.get("关联采购明细"))
        if mids:
            fields["关联采购明细"] = mids
        r = pp.feishu_api("POST", f"{BASE}/tables/{RECV}/records", {"fields": fields})
        rid2 = r.get("data", {}).get("record", {}).get("record_id")
        if rid2:
            put_fields(RECV, rid2, {"目的仓类型": whtype})   # 单选独立 PUT
            put_fields(RECV, rid2, {"入库状态": "待入库"})    # 单选独立 PUT
            n += 1
    print(f"  物化 {n} 行" + ("" if COMMIT else " [dry-run]"))

# ---------- 3. 主表阶段 roll-up ----------
def rollup_main():
    print("\n[3] 主表阶段 roll-up")
    mains = get_records(MAIN)
    contracts = {r["record_id"]: r["fields"] for r in get_records(CONTRACT)}
    picks = {r["record_id"]: r["fields"] for r in get_records(PICK)}
    recvs = {r["record_id"]: r["fields"] for r in get_records(RECV)}
    ships = {r["record_id"]: r["fields"] for r in get_records(SHIP)}
    n = 0
    for rec in mains:
        f = rec["fields"]; rid = rec["record_id"]
        cur = sel(f.get("阶段"))
        if cur not in INBOUND_STAGES:
            continue  # 不动合同签回之前的行
        # 收集本主表行关联的子实体
        my_contracts = [contracts[i] for i in link_ids(f.get("采购合同台-关联采购明细")) if i in contracts]
        my_picks     = [picks[i]     for i in link_ids(f.get("提货计划台-关联采购明细")) if i in picks]
        my_recvs     = [recvs[i]     for i in link_ids(f.get("入库登记台-关联采购明细")) if i in recvs]
        # 出货是否已出货(经 提货→出货批次)
        shipped = False
        for pf in my_picks:
            for sidp in link_ids(pf.get("关联出货批次")):
                if sel(ships.get(sidp, {}).get("出货状态")) == "已出货":
                    shipped = True
        # roll-up(取已达到的最高阶段)
        target = cur
        if any(sel(rf.get("入库状态")) == "已入库" for rf in my_recvs):
            target = ST_STOCKED
        elif any(sel(pf.get("分配状态")) == "运营已确认" for pf in my_picks):
            target = ST_PICKED
        elif shipped:
            target = ST_SHIPPED
        elif any(sel(cf.get("合同状态")) == "已下单生产" for cf in my_contracts):
            target = ST_ORDERED
        if target != cur:
            print(f"  {txt(f.get('产品名称'))} / {sel(f.get('站点'))} / {txt(f.get('月份'))}: {cur} → {target}")
            if put_fields(MAIN, rid, {"阶段": target}): n += 1
    print(f"  推进 {n} 行" + ("" if COMMIT else " [dry-run]"))

# ---------- 4. SLA 超时扫描(键于附属表日期, 旧订单/无主表也能跑) ----------
# 阈值(天, 可调; 提案默认)。分级: 超期/阈值 >1=L1提醒 / >2=L2部门负责人 / >3=L3 Frankie异常看板
SLA_SHIP_PLAN_DAYS = 7   # 计划出货日期 超期未出货 → 催采购(问供应商交期)
SLA_PICK_DAYS      = 3   # 实际出货后未提货采购确认 → 催采购+运营
SLA_RECV_DAYS      = 0   # 规定入库时间起超期未入库 → 催仓库
PROD_REMIND_LEAD   = 7   # 预计完工(下单+生产周期)前 N 天 → 提醒采购跟工厂确认+录出货
DAY_MS = 86400_000

def _days_over(date_ms, threshold_days, now_ms):
    if not date_ms: return None
    over = (now_ms - num(date_ms)) - threshold_days * DAY_MS
    return over / DAY_MS if over > 0 else None

def _level(days_over, threshold_days):
    base = max(threshold_days, 1)
    m = days_over / base
    return "L3·异常看板(Frankie)" if m > 3 else "L2·部门负责人" if m > 2 else "L1·提醒"

def sla_check(now_ms=None):
    # now_ms 可注入(测试用); 生产用当前时间。⚠️ Date.now 在脚本环境可用(Python time)。
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    print("\n[4] SLA 超时扫描(print-only; 通知发送待灰度验收件人)")
    ships = get_records(SHIP)
    picks = get_records(PICK)
    recvs = get_records(RECV)
    # 出货批次 -> 其提货行是否全部采购已确认
    pick_by_ship = {}
    for r in picks:
        for sid in link_ids(r["fields"].get("关联出货批次")):
            pick_by_ship.setdefault(sid, []).append(r["fields"])
    alerts = []
    # 生产跟催: 合同已下单生产 + 该合同无已出货批次 + 临近预计完工(下单+生产周期-提前7天) → 催采购跟工厂确认+录出货
    shipped_contract = set()
    for r in ships:
        if sel(r["fields"].get("出货状态")) == "已出货":
            for cid in link_ids(r["fields"].get("关联合同")):
                shipped_contract.add(cid)
    for r in get_records(CONTRACT):
        f = r["fields"]; cid = r["record_id"]
        if sel(f.get("合同状态")) != "已下单生产" or cid in shipped_contract:
            continue
        ot, pc = num(f.get("下单时间")), num(f.get("生产周期天数"))
        if not ot or not pc:
            continue
        expect_ship = ot + pc * DAY_MS
        ov = _days_over(ot, pc - PROD_REMIND_LEAD, now_ms)   # now ≥ 下单+生产周期-提前 → 触发
        if ov is not None:
            eta = (expect_ship - now_ms) / DAY_MS
            alerts.append((_level(ov, PROD_REMIND_LEAD),
                           f"合同 {txt(f.get('合同编号'))} 预计 {eta:+.0f} 天完工(下单+{pc:.0f}天) 仍未录出货",
                           "催采购跟工厂确认生产/出货进度 + 录出货计划"))
    for r in ships:
        f = r["fields"]; bn = txt(f.get("出货批次号")); st = sel(f.get("出货状态"))
        pn = txt(f.get("本批SKU及数量")); bd = f"{bn}" + (f"·{pn}" if pn else "")
        if st != "已出货":
            ov = _days_over(f.get("计划出货日期"), SLA_SHIP_PLAN_DAYS, now_ms)
            if ov is not None:
                alerts.append((_level(ov, SLA_SHIP_PLAN_DAYS), f"出货批次 {bd} 超计划出货 {ov:.0f}天未出货", "催采购(问供应商交期)"))
        else:
            prows = pick_by_ship.get(r["record_id"], [])
            all_conf = prows and all(sel(p.get("分配状态")) == "运营已确认" for p in prows)
            if not all_conf:
                ov = _days_over(f.get("实际出货日期"), SLA_PICK_DAYS, now_ms)
                if ov is not None:
                    alerts.append((_level(ov, SLA_PICK_DAYS), f"出货批次 {bd} 已出货 {ov:.0f}天 提货分配未采购确认", "催采购+运营"))
    for r in recvs:
        f = r["fields"]; rn = txt(f.get("入库登记号")); rsku = txt(f.get("ERP SKU"))
        rd = f"{rn}" + (f"·{rsku}" if rsku else "")
        if sel(f.get("入库状态")) != "已入库":
            ov = _days_over(f.get("规定入库时间"), SLA_RECV_DAYS, now_ms)
            if ov is not None:
                alerts.append((_level(ov, SLA_RECV_DAYS), f"入库登记 {rd} 超规定入库 {ov:.0f}天未入库", "催仓库"))
    if not alerts:
        print("  无超时项"); return alerts
    for lv, what, who in sorted(alerts, key=lambda a: a[0], reverse=True):
        print(f"  [{lv}] {what} → {who}")
    print("  ⚠ 铁律: 真发飞书前须 dry-run 验收件人(职务实时查 采购专员/仓库主管/运营)→单条→灰度, 本轮只 print。")
    return alerts

def run():
    mode = "COMMIT" if COMMIT else "DRY-RUN"
    print(f"=== 采购到货入库 B1 引擎 [{mode}] ===")
    archive_contracts()
    stamp_order()
    alloc_pickup()
    materialize_inbound()
    rollup_main()
    sla_check()
    print(f"\n=== 完成 [{mode}] ===")

if __name__ == "__main__":
    run()
