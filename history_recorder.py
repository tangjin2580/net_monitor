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
        # 原子写回：先写临时文件再 rename，避免半截文件
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            if kept:
                f.write("\n".join(kept) + "\n")
        os.replace(tmp, HISTORY_FILE)
    except OSError as e:
        logger.error("历史文件裁剪失败: %s", e)


def history_recorder() -> None:
    """后台线程: 每 HISTORY_INTERVAL 秒采集一次并落盘"""
    # 单例保护: 整个机器只允许一个记录器在跑。否则 web 多实例时会重复写
    # history.jsonl 并重复打 iKuai API, 把路由器 NAT 会话连接数放大约 N 倍。
    # 文件锁在进程退出时自动释放, 安全。
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
            # 1) 路由器 CPU/内存 (monitor_system 历史采样, 可能滞后 30~60 分钟)
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

            # 2) 实时 WAN 会话数 (来自 overview, 当前值, 用于即时发现风暴)
            live_conn = get_ikuai_live_conn_cached()

            # 3) 当前 ping RTT (从全局 stats 读最新值)
            with stats_lock:
                gw_rtt = _rtt_to_float(stats.get("gw_rtt"))
                v4_rtt = _rtt_to_float(stats.get("v4_rtt"))
                v6_rtt = _rtt_to_float(stats.get("v6_rtt"))

            rec = {
                "ts": now,
                "cpu": cpu,
                "mem": mem,
                "conn": conn_num,
                "live_conn": live_conn,
                "gw_rtt": gw_rtt,
                "v4_rtt": v4_rtt,
                "v6_rtt": v6_rtt,
            }
            _append_record(rec)

            # 每 TRIM_EVERY 次采集裁剪一次文件
            trim_counter += 1
            if trim_counter >= TRIM_EVERY:
                _trim_file()
                trim_counter = 0

        except Exception:  # noqa: BLE001 — 采集循环不能因单次异常退出
            logger.exception("历史采集异常")

        time.sleep(HISTORY_INTERVAL)


def read_history(days: int = HISTORY_DAYS, bucket_minutes: int = 30) -> dict[str, list]:
    """
    读取历史数据并按时间桶降采样，返回适合图表的结构。
    bucket_minutes: 聚合窗口(分钟)，默认30分钟 → 7天约336个点
    """
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
                        "gw_rtt": [], "v4_rtt": [], "v6_rtt": [],
                        "live_conn": [],
                    })
                    for k in ("cpu", "mem", "gw_rtt", "v4_rtt", "v6_rtt", "live_conn"):
                        v = rec.get(k)
                        if v is not None:
                            b[k].append(float(v))
    except OSError as e:
        logger.error("历史读取失败: %s", e)

    # 排序并求平均
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
