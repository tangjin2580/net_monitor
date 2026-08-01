"""_test_parser.py — log_parser._extract_stats_counts 的临时自测脚本

说明: 该脚本在开发机上直接 import 服务器部署目录(/opt/net_monitor)下的模块做冒烟测试，
属于一次性调试工具，不参与运行时。生产部署不需要此文件。
"""
from __future__ import annotations

import re
import sys

# 服务器上 net_monitor 模块的部署路径（仅本地测试用，非运行时依赖）
SERVER_MODULE_PATH = "/opt/net_monitor"
sys.path.insert(0, SERVER_MODULE_PATH)

from config import stats
from log_parser import _extract_stats_counts


def main() -> None:
    # Test 1: STATS 行解析
    test_msg = "状态: gw=UP v4=UP v6=UP"
    print("Before:", stats.get("v6_status"))
    _extract_stats_counts(test_msg, stats, "test")
    print("After STATS:", stats.get("v6_status"))

    # Test 2: HEARTBEAT RTT 推断
    stats["v6_status"] = "unknown"  # reset
    test_hb = "gw=0.224ms v4=23.1ms v6=22.7ms loss=0% qlty=100"
    for key, label in [
        ("gw_rtt", r"gw=(\d+\.?\d*)"),
        ("v4_rtt", r"v4=(\d+\.?\d*)"),
        ("v6_rtt", r"v6=(\d+\.?\d*)"),
    ]:
        m = re.search(label, test_hb)
        if m:
            stats[key] = m.group(1)
    print("v6_rtt:", stats.get("v6_rtt"))
    for rtt_key, status_key in [
        ("gw_rtt", "gw_status"),
        ("v4_rtt", "v4_status"),
        ("v6_rtt", "v6_status"),
    ]:
        val = stats.get(rtt_key)
        if val and val != "?":
            if stats.get(status_key, "unknown") in ("unknown", "down"):
                stats[status_key] = "up"
    print("v6_status after HB:", stats.get("v6_status"))


if __name__ == "__main__":
    main()
