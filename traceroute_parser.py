"""
traceroute_parser.py — Traceroute 文件解析与基线对比
"""
import os
import re
from datetime import datetime

from config import TRACE_BASE_DIR, TRACE_CUR_DIR, parsed_events

TRACE_LINE_RE = re.compile(r'^\s*(\d+)\s+(.+)$')


def parse_traceroute_file(filepath):
    """解析单个 traceroute 输出文件"""
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception:
        return None

    result = {"hops": [], "target": "", "raw_line_count": 0}
    for line in lines:
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("traceroute to "):
            m = re.match(r'traceroute to \S+ \((.+?)\)', line)
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
                ip_m = re.search(
                    r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|[0-9a-fA-F:]+:[0-9a-fA-F:]*)',
                    rest)
                rtt_m = re.search(r'([\d.]+)\s*ms', rest)
                ip = ip_m.group(1) if ip_m else "?"
                rtt = rtt_m.group(1) if rtt_m else "?"
                all_ips = re.findall(
                    r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|[0-9a-fA-F:]+:[0-9a-fA-F:]*)',
                    rest)
                ecmp = len(set(all_ips)) > 1 if all_ips else False
                result["hops"].append({
                    "n": hop_num, "ip": ip, "rtt": rtt,
                    "filtered": False, "ecmp": ecmp,
                    "all_ips": list(set(all_ips)) if ecmp else None,
                })
            result["raw_line_count"] += 1
    return result


def read_all_traces():
    """读取所有 traceroute 基线和当前数据"""
    data = {"baselines": {}, "current": {}, "change_events": []}
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
        if ev.get("event_type") in ("trace_v4_change", "trace_v6_change"):
            data["change_events"].append(ev)
    data["change_events"] = data["change_events"][-20:]
    return data
