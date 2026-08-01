"""
log_parser.py — 日志解析、事件分类、逐行处理、日志文件监控

优化要点:
- 预编译所有正则（原实现每次 process_line 都 re.search 字符串模式，热点路径重复编译）
- 统一 logging；类型注解；异常具体化
- 修复 _guess_mem 缓存永不过期的问题（改为带 TTL 的缓存）
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Match, Optional

from config import (
    LOG_FILE, LOG_PATTERN, DIAG_PATTERN,
    log_lines, parsed_events, stats, stats_lock,
    targets, diag_history, targets_lock, DIAG_HISTORY_MAX,
    broadcast_sse,
)

logger = logging.getLogger(__name__)

# 历史列表长度上限（消除魔法数字 100 / 200）
MAX_SYSTEM_STATS: int = 100
MAX_HTTP_LATENCIES: int = 100
MAX_RTT_HISTORY: int = 200
MAX_SUCCESS_RATE_HISTORY: int = 200

# ─── 预编译正则（性能：热点路径避免重复编译） ──────────────────────────
RE_PING_TOTAL = re.compile(r'total=(\d+)')
RE_PING_GW = re.compile(r'\bgw=(\d+)\b(?!\.)')
RE_PING_GW_OK = re.compile(r'gw_ok=(\d+)')
RE_PING_V4 = re.compile(r'\bv4=(\d+)\b(?!\.)')
RE_PING_V4_OK = re.compile(r'v4_ok=(\d+)')
RE_PING_INET_OK = re.compile(r'inet_ok=(\d+)')
RE_PING_V6 = re.compile(r'\bv6=(\d+)\b(?!\.)')
RE_PING_V6_OK = re.compile(r'v6_ok=(\d+)')
RE_STATS_NEW = re.compile(r'(gw|v4|v6)\s*=\s*(UP|DOWN|DEGRADED)', re.IGNORECASE)
RE_STATS_OLD = re.compile(r'(?:总|GW|V4|V6):\s*(\d+)')
RE_HEARTBEAT_RTT = {
    "gw_rtt": re.compile(r'gw=(\d+\.?\d*)'),
    "v4_rtt": re.compile(r'v4=(\d+\.?\d*)'),
    "v6_rtt": re.compile(r'v6=(\d+\.?\d*)'),
}
RE_QLTY = re.compile(r'qlty=(\d+)')
RE_SYSTEM_STATS = re.compile(r'cpu=(\d+)\s+mem=(\d+)\s+disk=(\d+)\s+load=([\d\.]+)')
RE_TCP_RETRANS = re.compile(r'rate=(\d+)')
RE_TCP_RETRANS_CN = re.compile(r'重传率(\d+)%')
RE_TCP_RETRANS_RATE = re.compile(r'rate=(\d+)%')
RE_BANDWIDTH = re.compile(r'rx=(\d+)kbps\s+tx=(\d+)kbps')
RE_HTTP_LATENCY = re.compile(r'(\S+)\s+→\s+(\d+)ms')


# ─── 日志行解析 ──────────────────────────────────────────────────────

def parse_log_line(line: str) -> Optional[Dict[str, str]]:
    m: Optional[Match[str]] = LOG_PATTERN.match(line.strip())
    if m:
        return {"ts": m.group(1), "level": m.group(2).strip(), "msg": m.group(3)}
    return None


def classify_event(parsed: Dict[str, str]) -> Optional[str]:
    msg = parsed["msg"]
    level = parsed["level"]
    if "V4_DOWN_V6_ALIVE" in msg: return "v4_down_v6_alive"
    if "V4_DOWN" in msg or "GW_DOWN" in msg: return "v4_down"
    if "V6_DOWN" in msg: return "v6_down"
    if "RECOVER_GW" in msg: return "recover_gw"
    if "RECOVER_V4" in msg: return "recover_v4"
    if "RECOVER_V6" in msg: return "recover_v6"
    if "SNAPSHOT" in msg: return "snapshot"
    if "V4_IP_LOST" in msg or "V4_IP_CHANGE" in msg: return "v4_ip_change"
    if "V6_IP_CHANGE" in msg: return "v6_ip_change"
    if "ROUTE_V4_CHANGE" in msg: return "route_v4_change"
    if "ROUTE_V6_CHANGE" in msg: return "route_v6_change"
    if "NO_V4_DEFAULT" in msg: return "no_v4_route"
    if "CONNTRACK_HIGH" in msg: return "conntrack_high"
    if "TRACE_V4_CHANGE" in msg: return "trace_v4_change"
    if "TRACE_V6_CHANGE" in msg: return "trace_v6_change"
    if "TRACE_BASE" in msg: return "trace_base"
    if "DNS_V4_FAIL" in msg or "DNS_V4_DOWN" in msg: return "dns_v4_fail"
    if "DNS_V6_FAIL" in msg: return "dns_v6_fail"
    if "BANDWIDTH" in msg: return "bandwidth"
    if "TCP_RETRANS" in msg: return "tcp_retrans"
    if "SYSTEM" in msg: return "system"
    if "LAN_PING" in msg: return "lan_ping"
    if "HTTP" in msg and "FAIL" not in msg: return "http_latency"
    # v1 兼容
    if "DISCONNECT" in msg: return "disconnect"
    if "INET_DOWN" in msg: return "inet_down"
    if "RECOVER" in msg: return "recover"
    if "LINK_DOWN" in msg: return "link_down"
    if "LINK_UP" in msg: return "link_up"
    if "IP_LOST" in msg or "IP_CHANGE" in msg: return "ip_change"
    if "ROUTE_CHANGE" in msg: return "route_change"
    if "MASS_LEAVE" in msg: return "mass_leave"
    if "ARP_LOST" in msg or "ARP_NEW" in msg or "ND_LOST" in msg or "ND_NEW" in msg: return "arp_change"
    if "DNS_FAIL" in msg or "DNS_DOWN" in msg: return "dns_fail"
    if "CARRIER_CHANGE" in msg: return "carrier_change"
    if level in ("ERROR", "WARN") and "FAIL" in msg: return "failure"
    if "HEARTBEAT" in msg: return "heartbeat"
    if "STATS" in msg: return "stats"
    return None


# ─── 解析辅助 ────────────────────────────────────────────────────────

def _apply_ping_stats(stats: Dict[str, Any], ts: str,
                      total: int, gw_ok: int, v4_ok: int, v6_ok: int) -> None:
    """应用解析到的 ping 统计到 stats 字典"""
    stats["total_pings"] = total
    stats["gw_ok"] = gw_ok
    stats["v4_ok"] = v4_ok
    stats["v6_ok"] = v6_ok
    stats["gw_status"] = "up"
    stats["v4_status"] = "up" if v4_ok > total * 0.8 else "degraded" if v4_ok > 0 else "down"
    stats["v6_status"] = "up" if v6_ok > total * 0.8 else "degraded" if v6_ok > 0 else "down"
    gw_rate = round(gw_ok / total * 100, 1) if total else 0
    v4_rate = round(v4_ok / total * 100, 1) if total else 0
    v6_rate = round(v6_ok / total * 100, 1) if total else 0
    stats["success_rate_history"].append({
        "ts": ts, "gw_rate": gw_rate, "v4_rate": v4_rate, "v6_rate": v6_rate,
    })
    stats["success_rate_history"] = stats["success_rate_history"][-MAX_SUCCESS_RATE_HISTORY:]


def _extract_ping_counts(msg: str, stats: Dict[str, Any], ts: str) -> None:
    """从 HEARTBEAT 扩展格式提取 ping 计数: total=N gw=X v4=Y v6=Z"""
    total_m = RE_PING_TOTAL.search(msg)
    gw_m = RE_PING_GW.search(msg) or RE_PING_GW_OK.search(msg)
    v4_m = RE_PING_V4.search(msg) or RE_PING_V4_OK.search(msg) or RE_PING_INET_OK.search(msg)
    v6_m = RE_PING_V6.search(msg) or RE_PING_V6_OK.search(msg)
    if total_m and gw_m and v4_m:
        _apply_ping_stats(stats, ts, int(total_m.group(1)), int(gw_m.group(1)),
                          int(v4_m.group(1)), int(v6_m.group(1)) if v6_m else 0)


def _extract_stats_counts(msg: str, stats: Dict[str, Any], ts: str) -> None:
    """从 [STATS] 行提取状态，支持两种格式:
    旧: 总: N | GW: P% (OK) | V4: Q% (OK) | V6: R% (OK)
    新: 状态: gw=UP v4=UP v6=UP  (或 DOWN/DEGRADED)
    """
    new_m = RE_STATS_NEW.findall(msg)
    if new_m:
        status_map = {"UP": "up", "DOWN": "down", "DEGRADED": "degraded"}
        for key, val in new_m:
            stats_key = f"{key.lower()}_status"
            stats[stats_key] = status_map.get(val.upper(), "unknown")
        return

    # 旧格式: 总: N | GW: P% (OK) | V4: Q% (OK) | V6: R% (OK)
    nums = RE_STATS_OLD.findall(msg)
    if len(nums) >= 3:
        total, gw_ok, v4_ok = (int(x) for x in nums[:3])
        v6_ok = int(nums[3]) if len(nums) > 3 else 0
        _apply_ping_stats(stats, ts, total, gw_ok, v4_ok, v6_ok)


def _append_rtt_history(stats: Dict[str, Any], ts: str) -> None:
    """从当前 stats 中取 RTT 值追加到历史"""
    try:
        gw_rtt_v = float(stats["gw_rtt"]) if stats.get("gw_rtt") not in ("?", "", None) else None
        v4_rtt_v = float(stats["v4_rtt"]) if stats.get("v4_rtt") not in ("?", "", None) else None
        v6_rtt_v = float(stats["v6_rtt"]) if stats.get("v6_rtt") not in ("?", "", None) else None
    except (ValueError, TypeError):
        return
    stats["recent_rtt_history"].append({
        "ts": ts, "gw_rtt": gw_rtt_v, "v4_rtt": v4_rtt_v, "v6_rtt": v6_rtt_v
    })
    stats["recent_rtt_history"] = stats["recent_rtt_history"][-MAX_RTT_HISTORY:]


# ─── 内存猜测（带 TTL 缓存，避免永久陈旧） ────────────────────────────
_MEM_CACHE: Dict[str, Any] = {"value": 0, "ts": 0.0}
MEM_GUESS_TTL: int = 60


def _guess_mem() -> int:
    """当日志 mem=0 时，用 /proc/meminfo 实际读取（带 60s TTL 缓存）"""
    now = time.time()
    if _MEM_CACHE["value"] > 0 and (now - _MEM_CACHE["ts"]) < MEM_GUESS_TTL:
        return _MEM_CACHE["value"]
    try:
        total_kb = avail_kb = 0
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                if parts[0] == "MemTotal:":
                    total_kb = int(parts[1])
                elif parts[0] == "MemAvailable:":
                    avail_kb = int(parts[1])
                    break
        if total_kb > 0:
            used_pct = round((total_kb - avail_kb) / total_kb * 100)
            _MEM_CACHE.update(value=used_pct, ts=now)
            return used_pct
    except OSError:
        pass
    return 72


# ─── 核心处理 ────────────────────────────────────────────────────────

def process_line(line: str) -> None:
    """解析一行日志，更新全局状态，广播 SSE"""
    parsed = parse_log_line(line)
    if not parsed:
        return

    log_lines.append(parsed)
    evt_type = classify_event(parsed)
    if evt_type:
        parsed["event_type"] = evt_type
        parsed_events.append(parsed)

    msg = parsed["msg"]
    level = parsed["level"]
    _is_hb = "HEARTBEAT" in msg or level == "HEARTBEAT"
    _is_stats = ("STATS" in msg and ("总:" in msg or "状态:" in msg)) or level == "STATS"

    # 1. HEARTBEAT 行 → 提取 RTT 延迟
    if _is_hb:
        with stats_lock:
            stats["last_heartbeat"] = parsed["ts"]
            for key, rx in RE_HEARTBEAT_RTT.items():
                m = rx.search(msg)
                if m:
                    stats[key] = m.group(1)
            # 从 RTT 推断状态: 有 RTT 值 = up, 无 = down
            # (HEARTBEAT 比 STATS 更频繁, 可及时更新状态)
            for rtt_key, status_key in [("gw_rtt", "gw_status"),
                                        ("v4_rtt", "v4_status"),
                                        ("v6_rtt", "v6_status")]:
                val = stats.get(rtt_key)
                if val and val != "?":
                    if stats.get(status_key, "unknown") in ("unknown", "down"):
                        stats[status_key] = "up"
            qm = RE_QLTY.search(msg)
            if qm:
                stats["link_quality"] = int(qm.group(1))
            _extract_ping_counts(msg, stats, parsed["ts"])
            _append_rtt_history(stats, parsed["ts"])

    # 2. STATS 行 → 提取 ping 成功率
    if _is_stats:
        with stats_lock:
            _extract_stats_counts(msg, stats, parsed["ts"])

    # 3. 系统资源
    if "SYSTEM_STATS" in msg:
        m = RE_SYSTEM_STATS.search(msg)
        if m:
            entry = {
                "ts": parsed["ts"].split(".")[0] if "." in parsed["ts"] else parsed["ts"],
                "cpu_pct": int(m.group(1)),
                "mem_pct": max(int(m.group(2)), _guess_mem()),
                "disk_pct": int(m.group(3)),
                "load1": float(m.group(4)),
            }
            with stats_lock:
                stats["system_stats"].append(entry)
                if len(stats["system_stats"]) > MAX_SYSTEM_STATS:
                    stats["system_stats"] = stats["system_stats"][-MAX_SYSTEM_STATS:]

    # 4. TCP 重传率（兼容英文 rate= 与中文 重传率 两种写法，合并为一段）
    if "TCP_RETRANS" in msg or ("TCP" in msg and "重传率" in msg):
        bm = RE_TCP_RETRANS_CN.search(msg) or RE_TCP_RETRANS_RATE.search(msg)
        if bm:
            with stats_lock:
                stats["tcp_retrans_rate"] = int(bm.group(1))

    # 5. 带宽
    if "BANDWIDTH" in msg:
        bm = RE_BANDWIDTH.search(msg)
        if bm:
            with stats_lock:
                stats["bandwidth_rx_kbps"] = int(bm.group(1))
                stats["bandwidth_tx_kbps"] = int(bm.group(2))

    # 6. HTTP 延迟
    if "HTTP" in msg and "ms" in msg and "FAIL" not in msg:
        hm = RE_HTTP_LATENCY.search(msg)
        if hm:
            with stats_lock:
                stats["http_latencies"].append({
                    "ts": parsed["ts"], "target": hm.group(1), "ms": int(hm.group(2))
                })
                stats["http_latencies"] = stats["http_latencies"][-MAX_HTTP_LATENCIES:]

    # 7. 断网事件计数
    down_keywords = ["DISCONNECT", "INET_DOWN", "LINK_DOWN", "V4_DOWN", "V6_DOWN",
                     "GW_DOWN", "V4_DOWN_V6_ALIVE", "NO_V4_DEFAULT", "CONNTRACK_HIGH"]
    if any(kw in msg for kw in down_keywords):
        with stats_lock:
            stats["disconnect_events"] += 1
            if any(kw in msg for kw in ["GW_DOWN", "LINK_DOWN"]):
                stats["gw_status"] = "down"
            if any(kw in msg for kw in ["V4_DOWN", "V4_DOWN_V6_ALIVE", "NO_V4_DEFAULT",
                                         "INET_DOWN", "DISCONNECT"]):
                stats["v4_status"] = "down"
            if "V6_DOWN" in msg:
                stats["v6_status"] = "down"

    if "RECOVER" in msg:
        with stats_lock:
            if "RECOVER_GW" in msg or ("网关" in msg and "recover" in msg.lower()):
                stats["gw_status"] = "up"
            if "RECOVER_V4" in msg or "RECOVER" in msg:
                stats["v4_status"] = "up"
            if "RECOVER_V6" in msg:
                stats["v6_status"] = "up"

    # 8. 逐服务诊断
    dm = DIAG_PATTERN.search(msg)
    if dm:
        cat = dm.group(1).strip()
        ip = dm.group(2).strip()
        status = dm.group(3).strip().lower()
        rtt = dm.group(4).strip()
        is_ok = status == "ok"
        with targets_lock:
            targets[cat] = {"ip": ip, "status": status, "rtt": rtt, "ts": parsed["ts"]}
            if cat not in diag_history:
                diag_history[cat] = []
            diag_history[cat].append({"ok": is_ok, "ts": parsed["ts"]})
            diag_history[cat] = diag_history[cat][-DIAG_HISTORY_MAX:]
            hist = diag_history[cat]
            ok_count = sum(1 for h in hist if h["ok"])
            targets[cat]["rate"] = round(ok_count / len(hist) * 100) if hist else 0
            targets[cat]["total"] = len(hist)

    broadcast_sse(parsed)


# ─── 日志文件监控线程 ────────────────────────────────────────────────

def log_watcher() -> None:
    """后台线程: 持续 tail 日志文件，逐行处理"""
    last_pos = 0
    while True:
        try:
            if os.path.exists(LOG_FILE):
                size = os.path.getsize(LOG_FILE)
                if size < last_pos:
                    last_pos = 0
                if size > last_pos:
                    with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
                        f.seek(last_pos)
                        for line in f:
                            process_line(line)
                        last_pos = f.tell()
        except OSError as e:
            logger.error("日志监控异常: %s", e)
        time.sleep(1)
