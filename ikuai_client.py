"""
ikuai_client.py — 爱快路由器 API 客户端
登录、调用 API、获取概览/WAN 状态
"""
import hashlib
import time

from config import (
    IKUAI_HOST, IKUAI_CALL_URL, IKUAI_USER, IKUAI_PASS,
)

# ─── 会话缓存 ────────────────────────────────────────────────────────
_ikuai_sess = {"sess": None, "login_time": 0}
_ikuai_wan_cache = {"ts": 0, "data": None}


def _ikuai_login():
    """账号密码登录，返回 requests.Session（含正确 cookie）"""
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
        timeout=10,
    )
    d = r.json()
    if d.get("code") == 0:
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
    调用爱快 API
    端点:  POST /Action/call
    认证:  cookie(sess_key+username+login) + body 里带 username/passwd
    响应:  {"code":0, "message":"Success", "results": {...}}
    """
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
        "isAjax": "1",
        "Origin": f"http://{IKUAI_HOST}",
        "Referer": f"http://{IKUAI_HOST}/",
    }
    try:
        r = sess.post(IKUAI_CALL_URL, json=body, headers=headers, timeout=5)
        result = r.json()
        code = result.get("code")
        # 认证失效 → 重新登录并重试一次
        if code in (1003, 1005, 1006):
            print(f"[iKuai] 认证失效({code})，重新登录...")
            _ikuai_sess["sess"] = None
            sess = _ikuai_login()
            if sess:
                r2 = sess.post(IKUAI_CALL_URL, json=body, headers=headers, timeout=5)
                result = r2.json()
        return result
    except Exception as e:
        return {"error": str(e)}


def get_ikuai_overview():
    """获取爱快路由器概览（使用已验证可用的 API）"""
    result = {"online": False, "wan_list": [], "iface_list": [],
              "lease_total": 0, "arp_total": 0}
    apis = [
        ("monitor_iface", "show", {"TYPE": "iface_stream"}),
        ("wan",            "show", None),
        ("lan",            "show", None),
        ("dhcp_server",    "show", {"TYPE": "data"}),
        ("dhcp_lease",     "show", None),
        ("arp",            "show", None),
    ]
    ok = False
    for fn, ac, pm in apis:
        r = ikuai_call(fn, ac, pm)
        if r.get("error"):
            result[fn] = {"error": r["error"]}
            continue
        res = r.get("results")
        if r.get("code") == 0 and res:
            result[fn] = res
            if fn == "wan" and res.get("data"):
                for w in res["data"]:
                    result["wan_list"].append({
                        "name":         w.get("name", w.get("tagname", "?")),
                        "pppoe_status": w.get("pppoe_status", 0),
                        "internet":     w.get("internet", 0),
                        "ip_addr":      w.get("pppoe_ip_addr", ""),
                        "dns":          w.get("dns", ""),
                    })
                    if w.get("pppoe_status") == 2 or w.get("internet") == 2:
                        ok = True
            elif fn == "monitor_iface" and res.get("iface_stream"):
                for i in res["iface_stream"]:
                    result["iface_list"].append({
                        "interface": i.get("interface", "?"),
                        "upload":   i.get("upload", 0),
                        "download": i.get("download", 0),
                    })
            elif fn == "dhcp_lease" and res.get("data"):
                result["lease_total"] = len(res["data"])
            elif fn == "arp" and res.get("data"):
                result["arp_total"] = len(res["data"])
        else:
            result[fn] = {"code": r.get("code"), "raw": r}
    result["online"] = ok
    return result


def get_ikuai_wan_status():
    """
    获取爱快 WAN 口状态（路由器自身的网络检测结果，权重最高）
    结果缓存 30 秒，避免每次请求都调 API
    """
    global _ikuai_wan_cache
    now = time.time()
    if _ikuai_wan_cache and (now - _ikuai_wan_cache.get("ts", 0)) < 30:
        return _ikuai_wan_cache["data"]
    try:
        r = ikuai_call("wan", "show")
        if r.get("code") != 0:
            result = {"error": r.get("message", "API调用失败"), "source": "ikuai"}
        else:
            data = r.get("results", {}).get("data", [])
            wan_list = []
            all_online = True
            for w in data:
                pppoe_status = w.get("pppoe_status", 0)
                internet     = w.get("internet", 0)
                name         = w.get("name", w.get("tagname", "?"))
                ip_addr      = w.get("pppoe_ip_addr", w.get("ip_addr", ""))
                up = (pppoe_status == 2) or (internet == 2)
                if not up:
                    all_online = False
                wan_list.append({
                    "name": name, "pppoe_status": pppoe_status,
                    "internet": internet, "online": up,
                    "ip_addr": ip_addr, "dns": w.get("dns", ""),
                })
            result = {"wan_list": wan_list, "all_online": all_online, "source": "ikuai"}
        _ikuai_wan_cache = {"ts": now, "data": result}
        return result
    except Exception as e:
        result = {"error": str(e), "source": "ikuai"}
        _ikuai_wan_cache = {"ts": now, "data": result}
        return result


def get_cached_wan_status():
    """获取缓存的 WAN 状态（不自动刷新，供外部读取缓存用）"""
    return _ikuai_wan_cache.get("data")


def refresh_wan_cache():
    """刷新 WAN 状态缓存并返回结果"""
    result = get_ikuai_wan_status()
    _ikuai_wan_cache["ts"] = time.time()
    _ikuai_wan_cache["data"] = result
    return result


def get_ikuai_live_conn():
    """
    实时 WAN 会话数（NAT 连接数）。
    来源: overview 的 monitor_iface.iface_stream，是路由器**当前**值，
    不像 monitor_system 历史采样那样滞后 30~60 分钟。
    返回: int 或 None
    """
    try:
        ov = get_ikuai_overview()
        mi = ov.get("monitor_iface")
        if not isinstance(mi, dict):
            return None
        stream = mi.get("iface_stream") or []
        best = None
        for it in stream:
            name = str(it.get("interface", "")).lower()
            if name.startswith("wan"):
                cn = _to_num(it.get("connect_num"))
                if cn is not None:
                    # 优先 wan1；否则取第一个 wan
                    if name == "wan1" or best is None:
                        best = int(cn)
        return best
    except Exception:
        return None


# live_conn 短缓存 (overview 较重, 同进程内 30s 复用)
_live_conn_cache = {"ts": 0, "data": None}


def get_ikuai_live_conn_cached():
    """带 30s 缓存的实时 WAN 会话数"""
    global _live_conn_cache
    now = time.time()
    if _live_conn_cache["data"] is not None and (now - _live_conn_cache["ts"]) < 30:
        return _live_conn_cache["data"]
    val = get_ikuai_live_conn()
    _live_conn_cache = {"ts": now, "data": val}
    return val


# ─── 路由器系统资源 (CPU/内存) ──────────────────────────────────────
_sysinfo_logged = False
# 30s 缓存: 降低 iKuai API 调用频率 (sysinfo 本就滞后 30~60 分钟, 短缓存无损)
_sysinfo_cache = {"ts": 0, "data": None}
_SYSINFO_CACHE_TTL = 30


def _to_num(val):
    """安全转 float，失败返回 None"""
    try:
        if val is None or val == "":
            return None
        return float(val)
    except (ValueError, TypeError):
        return None


def get_ikuai_sysinfo():
    """
    获取爱快路由器系统资源 (CPU/内存使用率)
    调用 monitor_system show，防御性解析不同固件的返回结构。

    爱快 monitor_system 返回 results.data 为列表(历史采样点)，
    每条含: cpu(浮点%), memory(已用KB), memory_use(百分比int),
            conn_num, on_terminal, timestamp, ...

    返回: {"cpu": float, "mem": int, "conn_num": int, "on_terminal": int,
           "wired_terminal": int, "wireless_terminal": int,
           "mem_total": int|None, "uptime": int|None, "error": str|None}
    """
    global _sysinfo_logged, _sysinfo_cache
    # 短缓存: 同一进程内 30s 内直接返回, 避免 web 每次请求都打 iKuai API
    now = time.time()
    if _sysinfo_cache["data"] is not None and (now - _sysinfo_cache["ts"]) < _SYSINFO_CACHE_TTL:
        cached = dict(_sysinfo_cache["data"])
        ts = cached.get("sysinfo_ts")
        cached["sysinfo_age"] = int(now - ts) if ts else None
        return cached
    result = {"cpu": None, "mem": None, "conn_num": None,
              "on_terminal": None, "wired_terminal": None, "wireless_terminal": None,
              "mem_total": None, "uptime": None,
              "sysinfo_ts": None, "sysinfo_age": None,
              "error": None}
    try:
        r = ikuai_call("monitor_system", "show")
        if r.get("error"):
            result["error"] = r["error"]
            return result
        if r.get("code") not in (0, None):
            result["error"] = r.get("message", "API调用失败(code=%s)" % r.get("code"))
            return result

        res = r.get("results", {}) or {}

        # 首次打印原始结构便于调试
        if not _sysinfo_logged:
            try:
                print("[iKuai] monitor_system 返回结构: %s" % str(res)[:500])
            except Exception:
                pass

        # 解析: results.data 可能是 list(爱快标准) 或 dict
        src = None
        if isinstance(res, dict):
            data = res.get("data")
            if isinstance(data, list) and data:
                # 取最后一条(最新)
                src = data[-1]
            elif isinstance(data, dict):
                src = data
            elif isinstance(res.get("sysinfo"), dict):
                src = res["sysinfo"]
            else:
                src = res

        if not isinstance(src, dict):
            result["error"] = "无法解析 monitor_system 返回结构"
            _sysinfo_logged = True
            return result

        # CPU: 爱快返回浮点百分比 (如 0.75, 19, 10.72)
        cpu = _to_num(src.get("cpu"))
        if cpu is None:
            cpu = _to_num(src.get("cpu_usage") or src.get("cpu_used"))
        result["cpu"] = round(cpu, 2) if cpu is not None else None

        # 内存百分比: memory_use 字段 (如 53, 54)
        mem = _to_num(src.get("memory_use"))
        if mem is None:
            mem = _to_num(src.get("mem_usage") or src.get("mem_used") or src.get("mem"))
        result["mem"] = round(mem) if mem is not None else None

        # 连接数和在线终端
        result["conn_num"] = _to_num(src.get("conn_num"))
        result["on_terminal"] = _to_num(src.get("on_terminal"))
        result["wired_terminal"] = _to_num(src.get("wired_terminal"))
        result["wireless_terminal"] = _to_num(src.get("wireless_terminal"))

        # 内存总量: 从 memory(KB已用) 和 memory_use(%) 反推
        mem_used_kb = _to_num(src.get("memory"))
        if mem_used_kb and mem and mem > 0:
            result["mem_total"] = round(mem_used_kb / (mem / 100.0))

        result["uptime"] = _to_num(src.get("uptime"))

        # 采样时间戳 + 滞后秒数 (monitor_system 每 30~60 分钟才采样, 数值可能很旧)
        _ts = _to_num(src.get("timestamp"))
        result["sysinfo_ts"] = _ts
        result["sysinfo_age"] = int(now - _ts) if _ts else None
        _sysinfo_logged = True

        # 成功结果才缓存 (失败不缓存, 让下次立即重试)
        _sysinfo_cache = {"ts": now, "data": result}
    except Exception as e:
        result["error"] = str(e)
    return result
