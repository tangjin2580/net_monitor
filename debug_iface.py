#!/usr/bin/env python3
"""debug_iface.py — 打印爱快 monitor_iface 接口数据的原始字段"""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.request
from http.cookiejar import CookieJar, Cookie

logger = logging.getLogger(__name__)

# ─── 配置（硬编码为本地诊断常量；正式运行请改为从配置文件读取）──────────
IKUAI_HOST = "192.168.31.254"
IKUAI_USER = "admin"
IKUAI_PASS = "lixin2324"
REQUEST_TIMEOUT = 10  # 单次请求超时(秒)

LOGIN_URL = f"http://{IKUAI_HOST}/Action/login"
CALL_URL = f"http://{IKUAI_HOST}/Action/call"


def login() -> CookieJar:
    """账号密码登录，返回携带会话 cookie 的 CookieJar"""
    jar = CookieJar()
    passwd_md5 = hashlib.md5(IKUAI_PASS.encode()).hexdigest()
    body = json.dumps(
        {"username": IKUAI_USER, "passwd": passwd_md5,
         "pass": "", "remember_password": True}
    ).encode("utf-8")
    handler = urllib.request.HTTPCookieProcessor(jar)
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(
        LOGIN_URL, data=body,
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
            d = json.loads(resp.read())
        logger.info("登录: code=%s", d.get("code"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.error("登录请求失败: %s", exc)
        raise
    for name, val in [("username", IKUAI_USER), ("login", "1")]:
        jar.set_cookie(Cookie(
            0, name, val, None, False,
            IKUAI_HOST, True, True, "/", True,
            False, None, False, None, None, {},
        ))
    return jar


def call(jar: CookieJar, func_name: str, action: str = "show",
         param: dict | None = None) -> dict:
    """调用爱快 Action/call 接口，返回解析后的 JSON dict"""
    passwd_md5 = hashlib.md5(IKUAI_PASS.encode()).hexdigest()
    body = {"username": IKUAI_USER, "passwd": passwd_md5,
            "func_name": func_name, "action": action}
    if param:
        body["param"] = param
    handler = urllib.request.HTTPCookieProcessor(jar)
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(
        CALL_URL, data=json.dumps(body).encode("utf-8"),
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json",
                 "isAjax": "1"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.error("接口 %s 调用失败: %s", func_name, exc)
        raise


def main() -> None:
    jar = login()

    print("\n=== monitor_iface TYPE=iface_stream (完整原始输出) ===\n")
    r = call(jar, "monitor_iface", "show", {"TYPE": "iface_stream"})
    print(json.dumps(r, ensure_ascii=False, indent=2))

    print("\n\n=== 各接口的完整字段 ===\n")
    if r.get("code") == 0:
        data = r.get("results", {}).get("iface_stream", [])
        for i, iface in enumerate(data):
            print(f"--- 接口 #{i + 1} ---")
            print(json.dumps(iface, ensure_ascii=False, indent=2))
            print()
    else:
        print("API 调用失败:", r)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    main()
