"""
traceroute_parser.py — Traceroute 文件解析与基线对比
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import TRACE_BASE_DIR, TRACE_CUR_DIR, parsed_events

logger = logging.getLogger(__name__)

TRACE_LINE_RE = re.compile(r'^\s*(\d+)\s+(.+)$')
TRACE_HEADER_RE = re.compile(r'traceroute to \S+ \((.+?)\)')
IP_RE = re.compile(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|[0-9a-fA-F:]+:[0-9a-fA-F:]*)')
RTT_RE = re.compile(r'([\d.]+)\s*ms')
CHANGE_EVENT_TYPES = ("trace_v4_change", "trace_v6_change")
CHANGE_EVENT_LIMIT = 20


def parse_traceroute_file(filepath: str) -> Optional[Dict[str, Any]]:
    """解析单个 traceroute 输出文件"""
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except OSError:
        return None

    result: Dict[str, Any] = {"hops": [], "target": "", "raw_line_count": 0}
    for line in lines:
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("traceroute to "):
            m = TRACE_HEADER_RE.match(line)
            if m:
                result["target"] = m.group(1)
            continue
        hm = TRACE_LINE_RE.match(line)
        if hm:
            hop_num = int(hm.group(1))
            rest = hm.group(2).strip()
            if '* * *' in rest:
                result["hops"].append({
                    "n": hop_num, "ip": "*", "rtt": None, "filtered": True
                })
            else:
                ip_m = IP_RE.search(rest)
                rtt_m = RTT_RE.search(rest)
                all_ips = IP_RE.findall(rest)
                ecmp = len(set(all_ips)) > 1
                result["hops"].append({
                    "n": hop_num,
                    "ip": ip_m.group(1) if ip_m else "?",
                    "rtt": rtt_m.group(1) if rtt_m else "?",
                    "filtered": False,
                    "ecmp": ecmp,
                    "all_ips": list(set(all_ips)) if ecmp else None,
                })
            result["raw_line_count"] += 1
    return result


def read_all_traces() -> Dict[str, Any]:
    """读取所有 traceroute 基线和当前数据"""
    data: Dict[str, Any] = {"baselines": {}, "current": {}, "change_events": []}
    for dir_path, key in [(TRACE_BASE_DIR, "baselines"), (TRACE_CUR_DIR, "current")]:
        if not os.path.isdir(dir_path):
            continue
        for fname in sorted(os.listdir(dir_path)):
            if not fname.endswith('.txt') or fname.endswith('.prev'):
                continue
            filepath = os.path.join(dir_path, fname)
            parsed = parse_traceroute_file(filepath)
            if parsed:
                parts = fname.replace('.txt', '').split('_', 1)
                proto = parts[0] if len(parts) > 1 else "?"
                target = parts[1] if len(parts) > 1 else fname
                parsed["protocol"] = proto.upper()
                parsed["file_target"] = target
                mtime = os.path.getmtime(filepath)
                parsed["ts"] = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
                data[key][fname.replace('.txt', '')] = parsed
    for ev in parsed_events:
        if ev.get("event_type") in CHANGE_EVENT_TYPES:
            data["change_events"].append(ev)
    data["change_events"] = data["change_events"][-CHANGE_EVENT_LIMIT:]
    return data
