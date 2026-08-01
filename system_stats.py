"""
system_stats.py — 系统资源采集线程 (CPU/内存/磁盘/负载/带宽)

优化要点:
- 统一 logging（原每 10s 向 stderr print 一次 → 改为 debug 级别）
- 异常具体化（OSError/ValueError），去除裸 except
- 采样间隔/历史上限等魔法数字提取为常量
- 类型注解
"""
from __future__ import annotations

import logging
import subprocess
import time
from typing import Any, Dict, List

from config import stats, stats_lock

logger = logging.getLogger(__name__)

# ─── 命名常量 ──────────────────────────────────────────────────────────
CPU_SAMPLE_INTERVAL: float = 0.5     # /proc/stat 两次采样间隔(秒)
COLLECT_INTERVAL: int = 10          # 采集周期(秒)
MAX_SYSTEM_STATS: int = 100         # system_stats 历史上限
PROC_STAT_FIELDS = 9                # /proc/stat 首行前 9 个字段用于 CPU 计算


def _cpu_usage() -> int:
    """两次采样 /proc/stat 计算 CPU 使用率(%)"""
    try:
        with open('/proc/stat') as f:
            fields1 = f.readline().split()
        time.sleep(CPU_SAMPLE_INTERVAL)
        with open('/proc/stat') as f:
            fields2 = f.readline().split()
        v1 = [int(x) for x in fields1[1:PROC_STAT_FIELDS]]
        v2 = [int(x) for x in fields2[1:PROC_STAT_FIELDS]]
        t1, i1 = sum(v1), v1[3] + v1[4]
        t2, i2 = sum(v2), v2[3] + v2[4]
        total_diff = t2 - t1
        idle_diff = i2 - i1
        if total_diff > 0:
            return max(0, min(100, int((total_diff - idle_diff) * 100 / total_diff)))
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _mem_usage() -> int:
    """从 /proc/meminfo 计算内存使用率(%)"""
    try:
        mi: Dict[str, int] = {}
        with open('/proc/meminfo') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mi[parts[0].rstrip(':')] = int(parts[1])
        total = mi.get('MemTotal', 0)
        avail = mi.get('MemAvailable', mi.get('MemFree', 0))
        if total > 0:
            return int((total - avail) * 100 / total)
    except (OSError, ValueError):
        pass
    return 0


def _disk_usage() -> int:
    """df / 计算根分区使用率(%)"""
    try:
        out = subprocess.check_output(['df', '/'], text=True).splitlines()
        if len(out) >= 2:
            return int(out[1].split()[-2].rstrip('%'))
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    return 0


def _load_avg() -> float:
    try:
        with open('/proc/loadavg') as f:
            return float(f.readline().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _default_iface() -> str:
    """从 ip route 解析默认出口网卡名"""
    try:
        routes = subprocess.check_output(['ip', 'route', 'show', 'default'], text=True)
        for rline in routes.splitlines():
            if 'dev ' in rline:
                return rline.split('dev ')[1].split()[0]
    except (subprocess.SubprocessError, OSError):
        pass
    return 'eth0'


def _bandwidth_kbps(iface: str) -> tuple[int, int]:
    """从 /proc/net/dev 读取累计字节数，返回 (rx_bytes, tx_bytes)"""
    try:
        with open('/proc/net/dev') as f:
            for line in f:
                if ':' in line and iface in line:
                    parts = line.split(':')[1].split()
                    return int(parts[0]), int(parts[8])
    except (OSError, ValueError, IndexError):
        pass
    return 0, 0


def system_stats_collector() -> None:
    """后台线程: 每 COLLECT_INTERVAL 秒采集一次系统资源，直接更新 stats"""
    while True:
        cpu_pct = _cpu_usage()
        mem_pct = _mem_usage()
        disk_pct = _disk_usage()
        load1 = _load_avg()

        iface = _default_iface()
        rx_bytes, tx_bytes = _bandwidth_kbps(iface)
        now = time.time()

        with stats_lock:
            entry: Dict[str, Any] = {
                "ts": time.strftime("%H:%M:%S"),
                "cpu_pct": cpu_pct,
                "mem_pct": mem_pct,
                "disk_pct": disk_pct,
                "load1": load1,
            }
            stats["system_stats"].append(entry)
            if len(stats["system_stats"]) > MAX_SYSTEM_STATS:
                stats["system_stats"] = stats["system_stats"][-MAX_SYSTEM_STATS:]

            # 带宽速率计算
            prev_rx = stats.get("prev_rx_bytes")
            prev_tx = stats.get("prev_tx_bytes")
            prev_t = stats.get("prev_rx_time")
            rx_kbps = tx_kbps = 0
            if prev_rx is not None and prev_t is not None and rx_bytes > 0:
                dt = now - prev_t
                if dt > 0.5:
                    rx_kbps = int((rx_bytes - prev_rx) * 8 / 1024 / dt)
                    tx_kbps = int((tx_bytes - prev_tx) * 8 / 1024 / dt)
            stats["prev_rx_bytes"] = rx_bytes
            stats["prev_tx_bytes"] = tx_bytes
            stats["prev_rx_time"] = now
            stats["bandwidth_rx_kbps"] = max(0, rx_kbps)
            stats["bandwidth_tx_kbps"] = max(0, tx_kbps)
            stats["link_quality"] = max(0, 100 - cpu_pct - mem_pct // 2)

        logger.debug("cpu=%s%% mem=%s%% disk=%s%% load=%.2f rx=%skbps tx=%skbps",
                     cpu_pct, mem_pct, disk_pct, load1,
                     stats["bandwidth_rx_kbps"], stats["bandwidth_tx_kbps"])
        time.sleep(COLLECT_INTERVAL)
