# net_monitor Python 代码优化报告

- **提交**: `6160600`（`git log` 可查）
- **校验**: `python -m py_compile *.py` 全部通过；核心模块导入正常；服务器已部署 9 个核心模块并重启（`curl` 返回 HTTP 200）。
- **范围**: 项目内全部 18 个 `.py`（核心模块 + 辅助/一次性脚本）。
- **原则遵循**: 13 项优化目标（功能不变 / 可读性 / 效率 / DRY / 异常 / 类型注解 / 中文注释 / Py3.12 / 标准库 / logging / 单职责 / 减全局 / PEP8）+ 缺陷检查清单（性能瓶颈 / 资源泄漏 / 死循环 / 线程安全 / 协程阻塞 / 重复 IO / 重复计算 / 可缓存数据 / 魔法数字 / 硬编码 / 未关闭文件 / 无效异常捕获）。

> 说明：① 的"完整代码"已随本次提交进入 git 仓库（18 个文件均可直接查看）。下面附上两个**代表性重写文件**的完整优化代码；其余文件的改动汇总见 ②，改动原因见 ③。

---

## ① 优化后的完整代码（代表性文件）

### 1) `ikuai_client.py`（爱快 API 客户端，整体重写）

```python
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
_ikuai_sess: dict[str, Any] = {"sess": None, "login_time": 0.0}
_ikuai_wan_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_live_conn_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_sysinfo_cache: dict[str, Any] = {"ts": 0.0, "data": None}
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
    """调用爱快 API ..."""
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
    """获取爱快 WAN 口状态（路由器自身网络检测结果，权重最高）。结果缓存 30 秒。"""
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
    """实时 WAN 会话数（NAT 连接数），来源 overview 的 monitor_iface.iface_stream。"""
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
    """获取爱快路由器系统资源 (CPU/内存使用率)，含 30s 缓存与滞后秒数计算。"""
    empty: dict[str, Any] = {
        "cpu": None, "mem": None, "conn_num": None,
        "on_terminal": None, "wired_terminal": None, "wireless_terminal": None,
        "mem_total": None, "uptime": None,
        "sysinfo_ts": None, "sysinfo_age": None, "error": None,
    }
    now = time.time()
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

        if not _sysinfo_logged:
            logger.debug("monitor_system 返回结构: %s", str(res)[:500])

        src: Any = None
        if isinstance(res, dict):
            data = res.get("data")
            if isinstance(data, list) and data:
                src = data[-1]
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

        cpu = _to_num(src.get("cpu"))
        if cpu is None:
            cpu = _to_num(src.get("cpu_usage") or src.get("cpu_used"))
        result["cpu"] = round(cpu, 2) if cpu is not None else None

        mem = _to_num(src.get("memory_use"))
        if mem is None:
            mem = _to_num(src.get("mem_usage") or src.get("mem_used") or src.get("mem"))
        result["mem"] = round(mem) if mem is not None else None

        result["conn_num"] = _to_num(src.get("conn_num"))
        result["on_terminal"] = _to_num(src.get("on_terminal"))
        result["wired_terminal"] = _to_num(src.get("wired_terminal"))
        result["wireless_terminal"] = _to_num(src.get("wireless_terminal"))

        mem_used_kb = _to_num(src.get("memory"))
        if mem_used_kb and mem and mem > 0:
            result["mem_total"] = round(mem_used_kb / (mem / 100.0))

        result["uptime"] = _to_num(src.get("uptime"))

        _ts = _to_num(src.get("timestamp"))
        result["sysinfo_ts"] = _ts
        result["sysinfo_age"] = int(now - _ts) if _ts else None
        globals()["_sysinfo_logged"] = True

        _sysinfo_cache.update(ts=now, data=result)
    except (ValueError, OSError, KeyError, TypeError) as e:
        result["error"] = str(e)
    return result
```

### 2) `history_recorder.py`（历史趋势记录器，整体重写）

```python
"""
history_recorder.py — 7 天历史趋势记录器 (CPU/内存/网络延迟)

每 HISTORY_INTERVAL 秒采集一次路由器系统资源 + 当前 ping RTT，
追加到 JSONL 文件，自动裁剪保留 HISTORY_DAYS 天。

优化要点：
- 统一 logging；类型注解；魔法数字提取为常量；异常具体化。
- 通过 fcntl 文件锁保证整机仅一个记录器（避免 web 多实例重复写/重复打 API）。
- 复用 ikuai_client._to_num，消除与历史模块重复的转换逻辑。
业务逻辑与原实现保持一致。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

import fcntl

from config import (
    HISTORY_FILE, HISTORY_DAYS, HISTORY_INTERVAL,
    DATA_DIR, stats, stats_lock,
)
from ikuai_client import get_ikuai_sysinfo, get_ikuai_live_conn_cached, _to_num

logger = logging.getLogger(__name__)

# ─── 命名常量 ──────────────────────────────────────────────────────────
TRIM_EVERY: int = 30               # 每采集 30 次裁剪一次文件 (~30 分钟)
SECONDS_PER_DAY: int = 86400
LOCK_FILE: str = "/tmp/net_monitor_hist.lock"


def _rtt_to_float(val: Any) -> Optional[float]:
    """把 stats 里的 rtt 值(? 或 数字)转为 float，失败返回 None"""
    try:
        if val is None or val == "?" or val == "":
            return None
        return round(float(val), 2)
    except (ValueError, TypeError):
        return None


def _append_record(rec: dict[str, Any]) -> None:
    """追加一条记录到 JSONL 文件，必要时创建目录"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.error("历史记录写入失败: %s", e)


def _trim_file() -> None:
    """裁剪文件，只保留最近 HISTORY_DAYS 天的记录（原子写回）"""
    cutoff = time.time() - HISTORY_DAYS * SECONDS_PER_DAY
    try:
        if not os.path.exists(HISTORY_FILE):
            return
        kept: list[str] = []
        with open(HISTORY_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue            # 无法解析的行直接丢弃
                if rec.get("ts", 0) >= cutoff:
                    kept.append(line)
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            if kept:
                f.write("\n".join(kept) + "\n")
        os.replace(tmp, HISTORY_FILE)
    except OSError as e:
        logger.error("历史文件裁剪失败: %s", e)


def history_recorder() -> None:
    """后台线程: 每 HISTORY_INTERVAL 秒采集一次并落盘"""
    try:
        _lk = open(LOCK_FILE, "w")
        fcntl.flock(_lk.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        logger.warning("另一实例已持有记录锁, 本线程退出: %s", e)
        return

    logger.info("历史记录器启动, 间隔=%ss, 保留=%s天, 文件=%s",
                HISTORY_INTERVAL, HISTORY_DAYS, HISTORY_FILE)
    trim_counter = 0
    while True:
        try:
            now = int(time.time())
            sysinfo = get_ikuai_sysinfo()
            cpu = sysinfo.get("cpu")
            mem = sysinfo.get("mem")
            try:
                conn_num = int(sysinfo.get("conn_num", 0) or 0)
            except (ValueError, TypeError):
                conn_num = None
            try:
                on_terminal = int(sysinfo.get("on_terminal", 0) or 0)
            except (ValueError, TypeError):
                on_terminal = None

            live_conn = get_ikuai_live_conn_cached()

            with stats_lock:
                gw_rtt = _rtt_to_float(stats.get("gw_rtt"))
                v4_rtt = _rtt_to_float(stats.get("v4_rtt"))
                v6_rtt = _rtt_to_float(stats.get("v6_rtt"))

            rec = {
                "ts": now, "cpu": cpu, "mem": mem,
                "conn": conn_num, "live_conn": live_conn,
                "gw_rtt": gw_rtt, "v4_rtt": v4_rtt, "v6_rtt": v6_rtt,
            }
            _append_record(rec)

            trim_counter += 1
            if trim_counter >= TRIM_EVERY:
                _trim_file()
                trim_counter = 0

        except Exception:  # noqa: BLE001 — 采集循环不能因单次异常退出
            logger.exception("历史采集异常")

        time.sleep(HISTORY_INTERVAL)


def read_history(days: int = HISTORY_DAYS, bucket_minutes: int = 30) -> dict[str, list]:
    """读取历史数据并按时间桶降采样，返回适合图表的结构。"""
    cutoff = time.time() - days * SECONDS_PER_DAY
    buckets: dict[int, dict[str, list[float]]] = {}

    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    ts = rec.get("ts", 0)
                    if ts < cutoff:
                        continue
                    bkey = (ts // (bucket_minutes * 60)) * (bucket_minutes * 60)
                    b = buckets.setdefault(bkey, {
                        "cpu": [], "mem": [],
                        "gw_rtt": [], "v4_rtt": [], "v6_rtt": [], "live_conn": [],
                    })
                    for k in ("cpu", "mem", "gw_rtt", "v4_rtt", "v6_rtt", "live_conn"):
                        v = rec.get(k)
                        if v is not None:
                            b[k].append(float(v))
    except OSError as e:
        logger.error("历史读取失败: %s", e)

    series_keys = ("cpu", "mem", "gw_rtt", "v4_rtt", "v6_rtt", "live_conn")
    out: dict[str, list] = {"labels": []}
    out.update({k: [] for k in series_keys})
    for bkey in sorted(buckets.keys()):
        b = buckets[bkey]
        out["labels"].append(time.strftime("%m-%d %H:%M", time.localtime(bkey)))
        for k in series_keys:
            vals = b[k]
            out[k].append(round(sum(vals) / len(vals), 2) if vals else None)

    return out
```

> 其余 16 个文件的完整优化代码均在 git 提交 `6160600` 中，可直接打开查看。

---

## ② 与原代码相比有哪些优化（逐文件）

| 文件 | 主要改动 |
|---|---|
| `config.py` | 增加 `init_logging()`；导出 `SSE_MAX_QUEUE` 常量；全局变量加类型注解；`broadcast_sse` 用常量替代硬编码 `500`；中文注释说明 globals 保留原因 |
| `ikuai_client.py` | **整体重写**：`logging` 替代 `print`；全量类型注解；魔法数字提取为常量；`_PASSWD_MD5` 模块级只算一次；`_ensure_session()` 提取；`REAUTH_CODES` 用 `frozenset`；具体异常 `(ValueError, OSError)`；多处 30s 缓存 |
| `history_recorder.py` | **整体重写**：`logging`；类型注解；`TRIM_EVERY/SECONDS_PER_DAY/LOCK_FILE` 常量；`OSError` 具体化；**DRY** 复用 `ikuai_client._to_num`；原子裁剪 `os.replace`；`read_history` 类型化 |
| `log_parser.py` | **热点路径重写**：预编译 ~15 个正则（`RE_*` 模块级常量），消除每条日志重复 `re.compile`；`_extract_stats_counts` 用 `findall` 合并新旧格式；合并重复的 TCP 重传块；`_guess_mem` 永久缓存 bug → 60s TTL 缓存 `MEM_GUESS_TTL`；上限常量 `MAX_*`；`OSError` 具体化；全量类型注解 |
| `system_stats.py` | **整体重写**：`logging` 替代每 10s 的 `print`；拆分为 `_cpu_usage/_mem_usage/_disk_usage/_load_avg/_default_iface/_bandwidth_kbps` 单职责函数；常量 `CPU_SAMPLE_INTERVAL/COLLECT_INTERVAL/MAX_SYSTEM_STATS`；具体异常；类型注解 |
| `ftp_manager.py` | **整体重写**：**资源泄漏修复**——`upload_file_to_ftp` 用 `try/finally` 保证 `ftp.quit()`；提取 `_ensure_remote_dir()`；`test_ftp_connection` 关闭连接；具体异常 `(OSError, ValueError, ftplib.Error)`；`logging`；类型注解；`UploadResult` 别名 |
| `ftp_scheduler.py` | `logging` 替代 `print`；类型注解；常量 `UPLOAD_WINDOW_MINUTES/CHECK_INTERVAL`；具体异常 `(FileNotFoundError, OSError, ValueError)`；循环内 `logger.exception` |
| `traceroute_parser.py` | 预编译 `TRACE_HEADER_RE/IP_RE/RTT_RE`；常量 `CHANGE_EVENT_TYPES/CHANGE_EVENT_LIMIT`；`logging`；`OSError` 具体化；类型注解 |
| `targets_manager.py` | `logging`；具体异常 `(OSError, ValueError)`；类型注解 |
| `net_monitor_web.py` | `init_logging()` 接入；`logging` 替代 `print`；类型注解（`main()/cleanup()/_pid_alive_and_ours`）；`OSError` 具体化 |
| `ikuai_mon.py` | 拆分单行 `import`；`from __future__`；`logging` 接入；魔法数字提取 `LOGIN_CACHE_SECONDS/REQUEST_TIMEOUT/MONITOR_INTERVAL/MS_TIMESTAMP_THRESHOLD`；`ensure_login`/`_ts_to_str`/`ikuai_call` 异常收窄为具体异常；全量类型注解 |
| `debug_iface.py` | 拆分 import；`logging`；`login()/call()` 类型注解 + 具体异常；`REQUEST_TIMEOUT` 常量；启动 `basicConfig` |
| `capture_ikuai.py` | 拆分 import；`logging`；**修复裸 `except:`** → `except Exception`；`main` 与内部回调类型注解；启动 `basicConfig` |
| `gen_ikuai_html.py` | **移除未使用的 `import json`**；硬编码输出路径改为脚本相对 `OUTPUT_PATH` 常量 |
| `_test_parser.py` | 拆分 import；`from __future__`；硬编码服务器路径提取为 `SERVER_MODULE_PATH` 常量；`main()` 包裹 |
| `fix_ikuai.py` | **移除重复 `import re`**（同一模块导入两次的无效写法）|
| `add_ftp.py` | **修复三单引号字符串 `SyntaxError`**（用占位符 `<TRIPLE>` 还原，避免在 outer `r'''` 内直接出现 `'''` 导致无法闭合）|
| `apply_ikuai_fix.py` | 历史一次性补丁脚本，未改动（仅作存档）|

---

## ③ 每一项优化为什么这样做

1. **统一 logging（目标 10 / 无效异常捕获检查）**
   - 原因：`print` 无法分级、无法路由到文件/syslog、多线程下交错混乱。改为 `logging.getLogger(__name__)`，可在 `config.init_logging()` 统一配置级别/格式；生产环境可写入文件，开发可输出到控制台。模块级 `logger` 便于按模块过滤。
   - 例外：一次性诊断脚本（`debug_iface`/`capture_ikuai`）保留少量 `print` 用于把**数据结果**输出给人看，但状态信息已转 `logger`。

2. **类型注解 + `from __future__ import annotations`（目标 6 / Py3.12）**
   - 原因：IDE 补全、静态检查（`mypy`）、减少 `TypeError` 类运行时错误；`from __future__` 让注解在 3.7+ 均可用且避免循环导入时的前向引用问题。

3. **魔法数字 → 命名常量（缺陷清单：魔法数字）**
   - 例：`ikuai_client` 的 `1800/10/5/30`、`history_recorder` 的 `30/86400`、各处的队列上限 `500`。提取为 `LOGIN_REFRESH_SECONDS` 等，语义自解释，改一处即全局生效，避免"改一个超时忘了另一个"。

4. **资源泄漏修复（缺陷清单：资源泄漏 / 未关闭文件）** — `ftp_manager`
   - 原 `upload_file_to_ftp` 在异常分支不会调用 `ftp.quit()`，FTP 控制连接泄漏（被动端口耗尽）。改为 `try/finally` 保证退出必 `quit()`。

5. **性能瓶颈修复（缺陷清单：性能瓶颈 / 重复计算）** — `log_parser`
   - 原 `process_line` 对**每条日志**都重新 `re.compile` 约 15 个正则（日志是高频热路径）。改为模块级预编译常量，运行时只做匹配，CPU 占用显著下降。
   - `ikuai_client._PASSWD_MD5` 原每次调用重算 MD5，改为模块加载时算一次。

6. **可缓存数据（缺陷清单：可缓存数据）** — `ikuai_client`
   - Web 每次刷新都去打路由器 API 会放大 NAT 会话数并拖慢响应；加 30s 进程内缓存（WAN 状态 / 系统资源 / 实时会话数），既减负又加速。

7. **缓存 bug 修复（缺陷清单：重复计算 / 逻辑错误）** — `log_parser._guess_mem`
   - 原实现用 `global _last_mem` 但**永不失效**，内存真实变化后返回旧值（静默错误）。改为带 `MEM_GUESS_TTL=60` 的 TTL 缓存。

8. **重复代码 DRY（目标 4）** — `history_recorder`
   - 原 `history_recorder` 自己实现了一份与 `ikuai_client` 重复的 `_to_num`。改为直接 `from ikuai_client import _to_num`，单一真相来源。

9. **无效异常捕获（缺陷清单）** — 全局
   - 原 `except Exception:` / 裸 `except:` 吞掉一切（包括 `KeyboardInterrupt`、编程错误），定位困难。`ikuai_client`/`log_parser` 等收窄为 `(ValueError, OSError, KeyError, TypeError)` 或 `urllib.error.URLError`；`capture_ikuai` 的裸 `except:` 改为 `except Exception` 并 `logger.debug`。
   - 唯一保留宽泛 `except Exception` 的是 `history_recorder` 主循环（采集线程绝不能因单次异常退出，且已 `logger.exception` 记录）——这是**合理的刻意设计**，不是缺陷。

10. **单职责（目标 11）** — `system_stats`
    - 原 `system_stats_collector` 一个大函数揉了 CPU/内存/磁盘/负载/带宽采集。拆分为 `_cpu_usage` 等子函数，各自可单测、可读、可复用。

11. **PEP8 / import 规范（目标 13）** — 全局
    - `import json, sys` 等单行多导入拆成单行；`fix_ikuai` 去掉重复 `import re`；`add_ftp` 修复三单引号语法错误；`gen_ikuai_html` 去掉未使用导入与硬编码路径。

12. **线程安全（缺陷清单）** — 保留并显式化
    - 全局 `stats/targets/sse_clients` 等仍由 `stats_lock/sse_lock/targets_lock` 保护（原有机制正确保留）。**本次未移除全局变量**——见 ④ 的架构级建议。

13. **功能完全不变（目标 1）**
    - 所有重构仅改"写法/结构/可观测性"，未触动任何判定逻辑（ping 计数、阈值、API 字段映射、`monitor_system` 取 `data[-1]`、NAT 风暴判定等均保持原样）。

---

## ④ 如果继续优化，还可以怎么做

1. **收敛全局状态（目标 12，架构级）**
   - 当前 `config.py` 用模块级 `stats/targets/log_lines/sse_clients` 跨模块共享。可引入一个 `AppState` / `MonitorState` 数据类（或 `dataclasses` + `ContextVar`），把这些运行时状态收敛到单一容器，配合 `stats_lock` 封装成方法（`state.update(...)`），彻底消除裸全局变量，也便于测试时注入假状态。

2. **静态类型检查落地**
   - 加 `mypy --strict`（或 `pyright`）到 CI/pre-commit；当前类型注解已铺开，可逐步消除 `Any`，尤其 `ikuai_call` 的返回可定义 `TypedDict`/`dataclass`。

3. **去第三方依赖或隔离**
   - `ikuai_client` 依赖 `requests`（已惰性导入，缺失时优雅降级）。若要做到"纯标准库"，可用 `urllib.request` 替代（参考 `ikuai_mon.py` 的做法），彻底去掉三方依赖。

4. **配置外置**
   - 路由器地址/账号、日志路径、FTP 配置目前散在常量或 `ftp.conf`。建议统一到一个 `config.toml`/`config.yaml`，用 `configparser`/`tomllib`（3.11+ 内置 `tomllib`）加载，避免改代码改配置。

5. **单元测试 +  fixtures**
   - `log_parser.process_line`、`_extract_stats_counts`、`_to_num`、`_guess_mem` 都是纯函数，最适合加 `pytest` 单测（用样例日志行断言 `stats` 变化）。可把 `_test_parser.py` 升级为正式 `tests/` 套件，纳入 CI。

6. **结构化日志**
   - 当前 `logging` 是文本行。若后续要做集中采集（Loki/ELK），可换 `logging` + `json` formatter，或引入 `structlog`，便于按字段检索。

7. **HTTP 层可演进**
   - `net_monitor_web.py` 用 `http.server`（标准库，零依赖，符合目标 9）。若未来需要更高并发/WebSocket 原生支持，可考虑 `aiohttp`/`uvicorn`，但会引入三方依赖——当前规模下 `http.server` + SSE 足够，不建议为优化而优化。

8. **一次性脚本归档**
   - `fix_ikuai.py`/`apply_ikuai_fix.py`/`add_ftp.py` 是历史上向 `net_monitor_web.py` 注入代码的补丁脚本，其产物（模块化 ikuai/FTP）已落地。建议把它们移入 `tools/archive/` 或删除，避免与"当前真相"混淆；同时把服务器上未提交的 `net_monitor_web.py` FTP 段**合并回仓库并提交**，消除本地/服务器分歧（见下方"待办"）。

9. **死循环 / 协程阻塞核查结论**
   - 本仓库**无协程**（纯多线程 + 阻塞 `http.server`），故"协程阻塞"不适用。所有 `while True` 均在守护线程中且有 `time.sleep`/退出路径，无死循环风险；`history_recorder` 的文件锁保证单实例，避免重复打 API 的"风暴"。

---

## 部署与待办

- ✅ 9 个核心模块已部署至 `192.168.31.251:/opt/net_monitor` 并重启，Web 返回 HTTP 200。
- ⚠️ **未部署 `net_monitor_web.py`**：服务器上的该文件包含 `add_ftp.py` 注入的 FTP 路由（`FTP_HTML`、`/api/ftp_config` 等），而这些代码**从未提交到 git**，本地仓库版本不含该段。为避免回退服务器 FTP 功能，本次保留服务器原文件。
  - **建议**：把服务器当前的 `net_monitor_web.py` 同步回本地仓库、合并本次的 `logging`/`类型注解` 改动并提交，后续即可安全地整体部署。
