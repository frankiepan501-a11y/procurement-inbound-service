#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端 shim — 替代 Frankie 主端 procurement_plan.py 的两个 helper(engine 只用这俩):
  - feishu_api(method, url, body=None, token=None)  默认用 app2(bitable) token
  - lx_api(path, params)                            领星走 n8n lingxing-proxy(出站IP已白名单)
凭据全走 env(public repo 零硬编)。其余 procurement_plan 的能力云端不需要。
"""
import os, json, time, urllib.request, urllib.error

E = os.environ.get
FEISHU_APP2_ID = E("FEISHU_APP2_ID", "")       # 聪哥2号 bitable 读写
FEISHU_APP2_SECRET = E("FEISHU_APP2_SECRET", "")
LINGXING_PROXY_URL = E("LINGXING_PROXY_URL", "")   # n8n /webhook/lingxing-proxy
LINGXING_PROXY_TOKEN = E("LINGXING_PROXY_TOKEN", "")
# 仓库映射表(站点/本地仓库名 → 领星 wid 单一真相源, 与 procurement_to_erp 同源; 非密钥)
WAREHOUSE_APP_TOKEN = E("WAREHOUSE_APP_TOKEN", "CjQAbEjzzaInS7sPbypcvp3Ynlf")
WAREHOUSE_TABLE_ID = E("WAREHOUSE_TABLE_ID", "tblBBOM07taRUtRQ")

_tok_cache = {}

def feishu_token(app_id, app_secret):
    c = _tok_cache.get(app_id)
    if c and c["expires"] > time.time():
        return c["token"]
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    r = json.loads(urllib.request.urlopen(req, timeout=20).read())
    token = r["tenant_access_token"]
    _tok_cache[app_id] = {"token": token, "expires": time.time() + r.get("expire", 7200) - 60}
    return token

def feishu_bitable_token():
    return feishu_token(FEISHU_APP2_ID, FEISHU_APP2_SECRET)

def feishu_api(method, url, body=None, token=None):
    if token is None:
        token = feishu_bitable_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", "ignore")
        print(f"  飞书API错误 {e.code}: {body_err[:200]}")
        return {"code": e.code, "msg": body_err[:200]}

def lx_api(path, params=None):
    """领星调用 — 走 n8n lingxing-proxy(服务端签名+IP白名单)。body={method,path,params}。"""
    if not LINGXING_PROXY_URL or not LINGXING_PROXY_TOKEN:
        return {"code": -1, "message": "lingxing proxy not configured", "data": []}
    body = {"method": "POST", "path": path, "params": params or {}}
    req = urllib.request.Request(LINGXING_PROXY_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {LINGXING_PROXY_TOKEN}", "Content-Type": "application/json"},
        method="POST")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=90).read())
    except urllib.error.HTTPError as e:
        return {"code": e.code, "message": e.read().decode("utf-8", "ignore")[:200], "data": []}
