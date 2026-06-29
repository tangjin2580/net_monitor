#!/usr/bin/env python3
"""精确修复 iKuai 模块（只替换 624-717 行，不动其他内容）"""
with open(r"G:\code\net_monitor\net_monitor_web.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

# 确认边界
assert "爱快路由器集成" in lines[623], f"行 624 不是 iKuai 开始！实际上是: {lines[623]}"
assert "class MonitorHandler" in lines[719], f"行 720 不是 MonitorHandler 开始！实际上是: {lines[719]}"
print(f"✅ 边界确认：624行='{lines[623].strip()}'  720行='{lines[719].strip()}'")

# 新 iKuai 代码（624-717 行，共 94 行）
new_lines = [
    "# ─── 爱快路由器集成 ──────────────────────────────────────────────\n",
    "IKUAI_HOST   = \"192.168.31.254\"\n",
    "IKUAI_CALL_URL = f\"http://{IKUAI_HOST}/Action/call\"   # 正确端点（浏览器抓包确认）\n",
    "IKUAI_USER   = \"admin\"\n",
    "IKUAI_PASS   = \"lixin2324\"\n",
    "IKUAI_TOKEN  = \"ZTVlNGNjZDUtNzkzMy00MzkyLWJkMjEt\"   # 系统设置→API令牌（暂未使用）\n",
    "\n",
    "_ikuai_sess = {\"sess\": None, \"login_time\": 0}   # 全局 session 缓存\n",
    "\n",
    "def _ikuai_login():\n",
    "    \"\"\"账号密码登录，返回 requests.Session（含正确 cookie）\"\"\"\n",
    "    import hashlib\n",
    "    try:\n",
    "        import requests as _req\n",
    "    except ImportError:\n",
    "        print(\"[iKuai] requests 未安装\")\n",
    "        return None\n",
    "    passwd_md5 = hashlib.md5(IKUAI_PASS.encode(\"utf-8\")).hexdigest()\n",
    "    sess = _req.Session()\n",
    "    r = sess.post(\n",
    "        f\"http://{IKUAI_HOST}/Action/login\",\n",
    "        json={\"username\": IKUAI_USER, \"passwd\": passwd_md5,\n",
    "              \"pass\": \"\", \"remember_password\": True},\n",
    "        headers={\"User-Agent\": \"Mozilla/5.0\"},\n",
    "        timeout=10\n",
    "    )\n",
    "    d = r.json()\n",
    "    if d.get(\"code\") == 0:\n",
    "        # 手动补上浏览器实际发送的三个 cookie\n",
    "        sess.cookies.set(\"username\", IKUAI_USER, domain=IKUAI_HOST)\n",
    "        sess.cookies.set(\"login\", \"1\", domain=IKUAI_HOST)\n",
    "        _ikuai_sess[\"sess\"] = sess\n",
    "        _ikuai_sess[\"login_time\"] = time.time()\n",
    "        print(\"[iKuai] 登录成功\")\n",
    "        return sess\n",
    "    print(f\"[iKuai] 登录失败: {d.get('message', '')}\")\n",
    "    return None\n",
    "\n",
    "def ikuai_call(func_name, action=\"show\", param=None):\n",
    "    \"\"\"\n",
    "    调用爱快 API（正确方式，浏览器抓包确认）\n",
    "    端点:  POST /Action/call\n",
    "    认证:  cookie(sess_key+username+login) + body 里带 username/passwd\n",
    "    响应:  {\"code\":0, \"message\":\"Success\", \"results\": {...}}   ← 注意是 results 不是 Data\n",
    "    \"\"\"\n",
    "    import hashlib\n",
    "    try:\n",
    "        import requests as _req\n",
    "    except ImportError:\n",
    "        return {\"error\": \"requests 未安装\"}\n",
    "    # 确保已登录（超过30分钟重新登录）\n",
    "    sess = _ikuai_sess.get(\"sess\")\n",
    "    if not sess or (time.time() - _ikuai_sess.get(\"login_time\", 0)) > 1800:\n",
    "        sess = _ikuai_login()\n",
    "        if not sess:\n",
    "            return {\"error\": \"iKuai 登录失败\"}\n",
    "    passwd_md5 = hashlib.md5(IKUAI_PASS.encode(\"utf-8\")).hexdigest()\n",
    "    body = {\"username\": IKUAI_USER, \"passwd\": passwd_md5,\n",
    "            \"func_name\": func_name, \"action\": action}\n",
    "    if param:\n",
    "        body[\"param\"] = param\n",
    "    headers = {\n",
    "        \"User-Agent\": \"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\",\n",
    "        \"Accept\": \"application/json, text/plain, */*\",\n",
    "        \"Content-Type\": \"application/json\",\n",
    "        \"isAjax\": \"1\",               # ← 小写！大写 IsAjax 会被拒绝\n",
    "        \"Origin\": f\"http://{IKUAI_HOST}\",\n",
    "        \"Referer\": f\"http://{IKUAI_HOST}/\",\n",
    "    }\n",
    "    try:\n",
    "        r = sess.post(IKUAI_CALL_URL, json=body, headers=headers, timeout=15)\n",
    "        result = r.json()\n",
    "        code = result.get(\"code\")\n",
    "        # 认证失效 → 重新登录并重试一次\n",
    "        if code in (1003, 1005, 1006):\n",
    "            print(f\"[iKuai] 认证失效({code})，重新登录...\")\n",
    "            _ikuai_sess[\"sess\"] = None\n",
    "            sess = _ikuai_login()\n",
    "            if sess:\n",
    "                r2 = sess.post(IKUAI_CALL_URL, json=body, headers=headers, timeout=15)\n",
    "                result = r2.json()\n",
    "        return result\n",
    "    except Exception as e:\n",
    "        return {\"error\": str(e)}\n",
    "\n",
    "def get_ikuai_overview():\n",
    "    \"\"\"获取爱快路由器概览（使用已验证可用的 API）\"\"\"\n",
    "    result = {\"online\": False}\n",
    "    # 已验证可用的 func_name（浏览器抓包 + 实际测试确认）：\n",
    "    #   monitor_iface  TYPE=iface_stream  → 各接口实时流量速度+总流量\n",
    "    #   wan                                  → WAN 口配置信息\n",
    "    #   lan                                  → LAN 口配置信息\n",
    "    #   dhcp_server     TYPE=data          → DHCP 服务配置\n",
    "    apis = [\n",
    "        (\"monitor_iface\", \"show\", {\"TYPE\": \"iface_stream\"}),\n",
    "        (\"wan\",            \"show\", None),\n",
    "        (\"lan\",            \"show\", None),\n",
    "        (\"dhcp_server\",    \"show\", {\"TYPE\": \"data\"}),\n",
    "    ]\n",
    "    ok = False\n",
    "    for fn, ac, pm in apis:\n",
    "        r = ikuai_call(fn, ac, pm)\n",
    "        if r.get(\"error\"):\n",
    "            result[fn] = {\"error\": r[\"error\"]}\n",
    "            continue\n",
    "        # 正确字段是 results（不是 Data！）\n",
    "        res = r.get(\"results\")\n",
    "        if r.get(\"code\") == 0 and res:\n",
    "            result[fn] = res\n",
    "            ok = True\n",
    "        else:\n",
    "            result[fn] = {\"code\": r.get(\"code\"), \"raw\": r}   # 保存原始响应供调试\n",
    "    result[\"online\"] = ok\n",
    "    return result\n",
    "\n",
]

# 替换：624-717 行（索引 623-716）
lines[623:717] = new_lines

with open(r"G:\code\net_monitor\net_monitor_web.py", "w", encoding="utf-8") as f:
    f.writelines(lines)

print(f"✅ 已替换 624-717 行（共 {717-623} 行 → {len(new_lines)} 行）")
print(f"   MonitorHandler 仍在：{lines[719].strip() if len(lines) > 719 else '???'}")
# 验证 MonitorHandler 还在
for i, ln in enumerate(lines):
    if "class MonitorHandler" in ln:
        print(f"✅ MonitorHandler 确认在行 {i+1}")
        break
else:
    print("❌ 错误：MonitorHandler 不见了！")
    exit(1)
