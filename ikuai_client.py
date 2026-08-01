"""
ikuai_client.py — 爱快路由器 API 客户端

负责：登录、调用 API、获取概览 / WAN 状态 / 系统资源 / 实时会话数。
设计要点：
- 复用单个 requests.Session（带 cookie），降低握手开销；
- 多处结果做短缓存（30s），避免 Web 每次请求都打路由器 API；
- 统一 logging，不再散用 print；
- 全部函数带类型注解，魔法数字提取为命名常量。
业务逻辑与原实现保持一致。
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Optional

from config import IKUAI_HOST, IKUAI_CALL_URL, IKUAI_USER, IKUAI_PASS

logger = logging.getLogger(__name__)

# ─── 命名常量（消除魔法数字） ──────────────────────────────────────────
LOGIN_REFRESH_SECONDS: int = 1800      # 登录态超过此时间重新登录
LOGIN_TIMEOUT: int = 10                # 登录请求超时(秒)
CALL_TIMEOUT: int = 5                  # 普通 API 调用超时(秒)
WAN_CACHE_TTL: int = 30                # WAN 状态缓存时长(秒)
SYSINFO_CACHE_TTL: int = 30            # 系统资源缓存时长(秒)
LIVE_CONN_CACHE_TTL: int = 30          # 实时会话数缓存时长(秒)
REAUTH_CODES = frozenset({1003, 1005, 1006})  # 认证失效需重登录的返回码
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 密码 MD5 只需算一次（原实现每次调用都重算）
_PASSWD_MD5: str = hashlib.md5(IKUAI_PASS.encode("utf-8")).hexdigest()


# ─── 模块级缓存（单例，进程内共享） ────────────────────────────────────
# 登录会话缓存
_ikuai_sess: dict[str, Any] = {"sess": None, "login_time": 0.0}
# WAN 状态缓存
_ikuai_wan_cache: dict[str, Any] = {"ts": 0.0, "data": None}
# 实时会话数缓存
_live_conn_cache: dict[str, Any] = {"ts": 0.0, "data": None}
# 系统资源缓存
_sysinfo_cache: dict[str, Any] = {"ts": 0.0, "data": None}
# 仅首次打印原始结构，便于调试
_sysinfo_logged: bool = False


def _require_requests() -> Any:
    """惰性导入 requests，缺失时抛 ImportError 由调用方处理"""
    import requests  # 可能 ImportError
    return requests


def _ikuai_login() -> Optional[Any]:
    """账号密码登录，返回带正确 cookie 的 requests.Session，失败返回 None"""
    try:
        requests = _require_requests()
    except ImportError:
        logger.error("requests 未安装，无法登录爱快")
        return None

    sess = requests.Session()
    try:
        r = sess.post(
            f"http://{IKUAI_HOST}/Action/login",
            json={"username": IKUAI_USER, "passwd": _PASSWD_MD5,
                  "pass": "", "remember_password": True},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=LOGIN_TIMEOUT,
        )
        d = r.json()
    except (ValueError, OSError) as e:
        logger.error("爱快登录请求失败: %s", e)
        return None

    if d.get("code") == 0:
        sess.cookies.set("username", IKUAI_USER, domain=IKUAI_HOST)
        sess.cookies.set("login", "1", domain=IKUAI_HOST)
        _ikuai_sess["sess"] = sess
        _ikuai_sess["login_time"] = time.time()
        logger.info("爱快登录成功")
        return sess
    logger.warning("爱快登录失败: %s", d.get("message", ""))
    return None


def _ensure_session() -> Optional[Any]:
    """确保已登录且登录态未过期，返回可用 Session 或 None"""
    sess = _ikuai_sess.get("sess")
    if sess is not None and (time.time() - _ikuai_sess.get("login_time", 0.0)) <= LOGIN_REFRESH_SECONDS:
        return sess
    return _ikuai_login()


def ikuai_call(func_name: str, action: str = "show", param: Optional[dict] = None) -> dict[str, Any]:
    """
    调用爱快 API。
    端点:  POST /Action/call
    认证:  cookie(sess_key+username+login) + body 带 username/passwd
    返回:  {"code":0, "message":"Success", "results": {...}} 或 {"error": "..."}
    """
    try:
        requests = _require_requests()
    except ImportError:
        return {"error": "requests 未安装"}

    sess = _ensure_session()
    if sess is None:
        return {"error": "iKuai 登录失败"}

    body = {"username": IKUAI_USER, "passwd": _PASSWD_MD5,
            "func_name": func_name, "action": action}
    if param:
        body["param"] = param
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "isAjax": "1",
        "Origin": f"http://{IKUAI_HOST}",
        "Referer": f"http://{IKUAI_HOST}/",
    }
    try:
        r = sess.post(IKUAI_CALL_URL, json=body, headers=headers, timeout=CALL_TIMEOUT)
        result = r.json()
    except (ValueError, OSError) as e:
        return {"error": str(e)}

    # 认证失效 → 重新登录并重试一次
    if result.get("code") in REAUTH_CODES:
        logger.info("认证失效(%s)，重新登录后重试", result.get("code"))
        _ikuai_sess["sess"] = None
        sess = _ikuai_login()
        if sess is None:
            return {"error": "iKuai 重新登录失败"}
        try:
            r2 = sess.post(IKUAI_CALL_URL, json=body, headers=headers, timeout=CALL_TIMEOUT)
            result = r2.json()
        except (ValueError, OSError) as e:
            return {"error": str(e)}
    return result


def get_ikuai_overview() -> dict[str, Any]:
    """获取爱快路由器概览（WAN/接口/终端等），聚合多个 API 的结果"""
    result: dict[str, Any] = {
        "online": False, "wan_list": [], "iface_list": [],
        "lease_total": 0, "arp_total": 0,
    }
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


def get_ikuai_wan_status() -> dict[str, Any]:
    """
    获取爱快 WAN 口状态（路由器自身网络检测结果，权重最高）。
    结果缓存 30 秒，避免每次请求都调 API。
    """
    now = time.time()
    if _ikuai_wan_cache.get("data") is not None and (now - _ikuai_wan_cache.get("ts", 0.0)) < WAN_CACHE_TTL:
        return _ikuai_wan_cache["data"]

    try:
        r = ikuai_call("wan", "show")
        if r.get("code") != 0:
            result: dict[str, Any] = {"error": r.get("message", "API调用失败"), "source": "ikuai"}
        else:
            data = r.get("results", {}).get("data", [])
            wan_list = []
            all_online = True
            for w in data:
                pppoe_status = w.get("pppoe_status", 0)
                internet = w.get("internet", 0)
                name = w.get("name", w.get("tagname", "?"))
                ip_addr = w.get("pppoe_ip_addr", w.get("ip_addr", ""))
                up = (pppoe_status == 2) or (internet == 2)
                if not up:
                    all_online = False
                wan_list.append({
                    "name": name, "pppoe_status": pppoe_status,
                    "internet": internet, "online": up,
                    "ip_addr": ip_addr, "dns": w.get("dns", ""),
                })
            result = {"wan_list": wan_list, "all_online": all_online, "source": "ikuai"}
        _ikuai_wan_cache.update(ts=now, data=result)
        return result
    except (ValueError, OSError, KeyError) as e:
        result = {"error": str(e), "source": "ikuai"}
        _ikuai_wan_cache.update(ts=now, data=result)
        return result


def get_cached_wan_status() -> Optional[dict[str, Any]]:
    """读取缓存的 WAN 状态（不自动刷新）"""
    return _ikuai_wan_cache.get("data")


def refresh_wan_cache() -> dict[str, Any]:
    """强制刷新 WAN 状态缓存并返回结果"""
    result = get_ikuai_wan_status()
    _ikuai_wan_cache["ts"] = time.time()
    _ikuai_wan_cache["data"] = result
    return result


def _to_num(val: Any) -> Optional[float]:
    """安全转 float，失败返回 None"""
    try:
        if val is None or val == "":
            return None
        return float(val)
    except (ValueError, TypeError):
        return None


def get_ikuai_live_conn() -> Optional[int]:
    """
    实时 WAN 会话数（NAT 连接数）。
    来源: overview 的 monitor_iface.iface_stream，是路由器**当前**值，
    不像 monitor_system 历史采样那样滞后 30~60 分钟。
    """
    try:
        ov = get_ikuai_overview()
        mi = ov.get("monitor_iface")
        if not isinstance(mi, dict):
            return None
        stream = mi.get("iface_stream") or []
        best: Optional[int] = None
        for it in stream:
            name = str(it.get("interface", "")).lower()
            if name.startswith("wan"):
                cn = _to_num(it.get("connect_num"))
                if cn is not None:
                    # 优先 wan1；否则取第一个 wan
                    if name == "wan1" or best is None:
                        best = int(cn)
        return best
    except (ValueError, OSError, KeyError, TypeError):
        return None


def get_ikuai_live_conn_cached() -> Optional[int]:
    """带 30s 缓存的实时 WAN 会话数"""
    now = time.time()
    if _live_conn_cache.get("data") is not None and (now - _live_conn_cache.get("ts", 0.0)) < LIVE_CONN_CACHE_TTL:
        return _live_conn_cache["data"]
    val = get_ikuai_live_conn()
    _live_conn_cache.update(ts=now, data=val)
    return val


def get_ikuai_sysinfo() -> dict[str, Any]:
    """
    获取爱快路由器系统资源 (CPU/内存使用率)。
    调用 monitor_system show，防御性解析不同固件返回结构。

    爱快 monitor_system 返回 results.data 为列表(历史采样点)，
    每条含: cpu(浮点%), memory(已用KB), memory_use(百分比int),
            conn_num, on_terminal, timestamp, ...

    返回: {"cpu": float, "mem": int, "conn_num": int, "on_terminal": int,
           "wired_terminal": int, "wireless_terminal": int,
           "mem_total": int|None, "uptime": int|None,
           "sysinfo_ts": float|None, "sysinfo_age": int|None, "error": str|None}
    """
    empty: dict[str, Any] = {
        "cpu": None, "mem": None, "conn_num": None,
        "on_terminal": None, "wired_terminal": None, "wireless_terminal": None,
        "mem_total": None, "uptime": None,
        "sysinfo_ts": None, "sysinfo_age": None, "error": None,
    }
    now = time.time()
    # 短缓存: 同一进程内 30s 内直接返回, 避免 Web 每次请求都打 iKuai API
    cached = _sysinfo_cache.get("data")
    if cached is not None and (now - _sysinfo_cache.get("ts", 0.0)) < SYSINFO_CACHE_TTL:
        out = dict(cached)
        ts = out.get("sysinfo_ts")
        out["sysinfo_age"] = int(now - ts) if ts else None
        return out

    result = dict(empty)
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
            logger.debug("monitor_system 返回结构: %s", str(res)[:500])

        # 解析: results.data 可能是 list(爱快标准) 或 dict
        src: Any = None
        if isinstance(res, dict):
            data = res.get("data")
            if isinstance(data, list) and data:
                src = data[-1]            # 取最后一条(最新)
            elif isinstance(data, dict):
                src = data
            elif isinstance(res.get("sysinfo"), dict):
                src = res["sysinfo"]
            else:
                src = res

        if not isinstance(src, dict):
            result["error"] = "无法解析 monitor_system 返回结构"
            globals()["_sysinfo_logged"] = True
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
        globals()["_sysinfo_logged"] = True

        # 成功结果才缓存 (失败不缓存, 让下次立即重试)
        _sysinfo_cache.update(ts=now, data=result)
    except (ValueError, OSError, KeyError, TypeError) as e:
        result["error"] = str(e)
    return result
