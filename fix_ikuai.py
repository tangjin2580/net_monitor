#!/usr/bin/env python3
"""修复 iKuai 模块：正确端点、正确响应字段、正确 Header"""
import re

with open(r"G:\code\net_monitor\net_monitor_web.py", "r", encoding="utf-8") as f:
    content = f.read()

# ── 1. 修复 IKUAI_API_URL → IKUAI_CALL_URL，端点改为 /Action/call ──────────
content = content.replace(
    'IKUAI_API_URL = f"http://{IKUAI_HOST}/Action/"',
    'IKUAI_CALL_URL = f"http://{IKUAI_HOST}/Action/call"   # 正确端点'
)

# ── 2. 删除整个旧的 ikuai_login / ikuai_call / get_ikuai_overview ──────
# 找到起始位置（# ─── 爱快路由器集成 ─）
start_marker = "# ─── 爱快路由器集成 ──────────────────────────────────────────────"
# 找到结束位置（下一个 # ─── 开头的行）
start_idx = content.find(start_marker)
if start_idx == -1:
    print("找不到起始标记！")
    exit(1)

# 找到下一个 # ─── 的行（即下一个模块的开始）
rest = content[start_idx:]
next_section_match = re.search(r'\n# ─── \w', rest)
if next_section_match:
    end_idx = start_idx + next_section_match.start()
else:
    print("找不到结束位置！")
    exit(1)

print(f"找到 iKuai 模块：{start_idx} ~ {end_idx}")
print(f"将替换约 {end_idx - start_idx} 字符")

# ── 3. 新实现 ────────────────────────────────────────────────────────────────
new_ikuai_code = '''# ─── 爱快路由器集成 ──────────────────────────────────────────────
IKUAI_HOST   = "192.168.31.254"
IKUAI_CALL_URL = f"http://{IKUAI_HOST}/Action/call"   # 正确端点（浏览器抓包确认）
IKUAI_USER   = "admin"
IKUAI_PASS   = "lixin2324"
IKUAI_TOKEN  = "ZTVlNGNjZDUtNzkzMy00MzkyLWJkMjEt"   # 系统设置→API令牌

# 全局 session（登录后复用，30分钟过期重登）
_ikuai_sess = {"sess": None, "login_time": 0}

def _ikuai_login():
    """账号密码登录，返回 requests.Session（含正确 cookie）"""
    import hashlib
    try:
        import requests as _req
    except ImportError:
        print("[iKuai] requests 未安装")
        return None
    passwd_md5 = hashlib.md5(IKUAI_PASS.encode("utf-8")).hexdigest()
    sess = _req.Session()
    r = sess.post(
        f"http://{IKUAI_HOST}/Action/login",
        json={"username": IKUAI_USER, "passwd": passwd_md5,
              "pass": "", "remember_password": True},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10
    )
    d = r.json()
    if d.get("code") == 0:
        # 手动补上浏览器实际发送的三个 cookie
        sess.cookies.set("username", IKUAI_USER, domain=IKUAI_HOST)
        sess.cookies.set("login", "1", domain=IKUAI_HOST)
        _ikuai_sess["sess"] = sess
        _ikuai_sess["login_time"] = time.time()
        print("[iKuai] 登录成功")
        return sess
    print(f"[iKuai] 登录失败: {d.get('message', '')}")
    return None

def ikuai_call(func_name, action="show", param=None):
    """
    调用爱快 API（正确方式，浏览器抓包确认）
    端点:  POST /Action/call
    认证:  cookie(sess_key+username+login) + body 里带 username/passwd
    响应:  {"code":0, "message":"Success", "results": {...}}   ← 注意是 results 不是 Data
    """
    import hashlib
    try:
        import requests as _req
    except ImportError:
        return {"error": "requests 未安装"}

    # 确保已登录（超过30分钟重新登录）
    sess = _ikuai_sess.get("sess")
    if not sess or (time.time() - _ikuai_sess.get("login_time", 0)) > 1800:
        sess = _ikuai_login()
        if not sess:
            return {"error": "iKuai 登录失败"}

    passwd_md5 = hashlib.md5(IKUAI_PASS.encode("utf-8")).hexdigest()
    body = {"username": IKUAI_USER, "passwd": passwd_md5,
            "func_name": func_name, "action": action}
    if param:
        body["param"] = param

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "isAjax": "1",               # ← 小写！大写 IsAjax 会被拒绝
        "Origin": f"http://{IKUAI_HOST}",
        "Referer": f"http://{IKUAI_HOST}/",
    }

    try:
        r = sess.post(IKUAI_CALL_URL, json=body, headers=headers, timeout=15)
        result = r.json()
        code = result.get("code")
        # 认证失效 → 重新登录并重试一次
        if code in (1003, 1005, 1006):
            print(f"[iKuai] 认证失效({code})，重新登录...")
            _ikuai_sess["sess"] = None
            sess = _ikuai_login()
            if sess:
                r2 = sess.post(IKUAI_CALL_URL, json=body, headers=headers, timeout=15)
                result = r2.json()
        return result
    except Exception as e:
        return {"error": str(e)}

def get_ikuai_overview():
    """获取爱快路由器概览（使用已验证可用的 API）"""
    result = {"online": False}
    # 已验证可用的 func_name（浏览器抓包 + 实际测试确认）：
    #   monitor_iface  TYPE=iface_stream  → 各接口实时流量速度+总流量
    #   wan                                  → WAN 口配置信息
    #   lan                                  → LAN 口配置信息
    #   dhcp_server     TYPE=data          → DHCP 服务配置
    apis = [
        ("monitor_iface", "show", {"TYPE": "iface_stream"}),
        ("wan",            "show", None),
        ("lan",            "show", None),
        ("dhcp_server",    "show", {"TYPE": "data"}),
    ]
    ok = False
    for fn, ac, pm in apis:
        r = ikuai_call(fn, ac, pm)
        if r.get("error"):
            result[fn] = {"error": r["error"]}
            continue
        # 正确字段是 results（不是 Data！）
        res = r.get("results")
        if r.get("code") == 0 and res:
            result[fn] = res
            ok = True
        else:
            # 保存原始响应供调试
            result[fn] = {"code": r.get("code"), "raw": r}
    result["online"] = ok
    return result

'''

content = content[:start_idx] + new_ikuai_code + content[end_idx:]

with open(r"G:\code\net_monitor\net_monitor_web.py", "w", encoding="utf-8") as f:
    f.write(content)

print("✅ iKuai 模块已更新")
print("  端点: /Action/call")
print("  响应字段: results（不是 Data）")
print("  Header: isAjax（小写）")
print("  func_name: monitor_iface / wan / lan / dhcp_server")
