#!/usr/bin/env python3
"""debug_iface.py — 打印爱快 monitor_iface 接口数据的原始字段"""
import json, hashlib, urllib.request
from http.cookiejar import CookieJar, Cookie

IKUAI_HOST = "192.168.31.254"
IKUAI_USER = "admin"
IKUAI_PASS = "lixin2324"

def login():
    jar = CookieJar()
    passwd_md5 = hashlib.md5(IKUAI_PASS.encode()).hexdigest()
    body = json.dumps({"username": IKUAI_USER, "passwd": passwd_md5,
                        "pass": "", "remember_password": True}).encode("utf-8")
    handler = urllib.request.HTTPCookieProcessor(jar)
    opener  = urllib.request.build_opener(handler)
    req    = urllib.request.Request(f"http://{IKUAI_HOST}/Action/login", data=body,
                    headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
                    method="POST")
    with opener.open(req, timeout=10) as resp:
        d = json.loads(resp.read())
        print(f"登录: code={d.get('code')}")
    for name, val in [("username", IKUAI_USER), ("login", "1")]:
        jar.set_cookie(Cookie(0, name, val, None, False, IKUAI_HOST, True, True, "/", True, False, None, False, None, None, {}))
    return jar

def call(jar, func_name, action="show", param=None):
    passwd_md5 = hashlib.md5(IKUAI_PASS.encode()).hexdigest()
    body = {"username": IKUAI_USER, "passwd": passwd_md5, "func_name": func_name, "action": action}
    if param:
        body["param"] = param
    handler = urllib.request.HTTPCookieProcessor(jar)
    opener  = urllib.request.build_opener(handler)
    req    = urllib.request.Request(f"http://{IKUAI_HOST}/Action/call", data=json.dumps(body).encode("utf-8"),
                    headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json", "isAjax": "1"},
                    method="POST")
    with opener.open(req, timeout=10) as resp:
        return json.loads(resp.read())

jar = login()

print("\n=== monitor_iface TYPE=iface_stream (完整原始输出) ===\n")
r = call(jar, "monitor_iface", "show", {"TYPE": "iface_stream"})
print(json.dumps(r, ensure_ascii=False, indent=2))

print("\n\n=== 各接口的完整字段 ===\n")
if r.get("code") == 0:
    data = r.get("results", {}).get("iface_stream", [])
    for i, iface in enumerate(data):
        print(f"--- 接口 #{i+1} ---")
        print(json.dumps(iface, ensure_ascii=False, indent=2))
        print()
else:
    print("API 调用失败:", r)
