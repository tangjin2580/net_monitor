"""
config.py — 全局配置常量 + 共享状态 + 线程锁
"""
import json
import os
import re
import threading
from collections import deque

# ─── 路径与端口 ──────────────────────────────────────────────────────
PORT = 9091
PID_FILE = "/opt/net_monitor/net_monitor_web.pid"
MONITOR_PID_FILE = "/home/li/net_monitor.pid"
LOG_DIR = "/home/li/net_monitor_logs"
LOG_FILE = os.path.join(LOG_DIR, "current.log")
MONITOR_LOG = LOG_FILE  # 兼容别名

WEBHOOK_CONF = "/opt/net_monitor/webhook.conf"
TRACE_BASE_DIR = os.path.join(LOG_DIR, "trace_baselines")
TRACE_CUR_DIR = os.path.join(LOG_DIR, "trace_current")

# ─── 历史趋势数据存储 (CPU/内存/延迟, 7天) ───────────────────────────
DATA_DIR = os.path.join(LOG_DIR, "data")
HISTORY_FILE = os.path.join(DATA_DIR, "history.jsonl")
HISTORY_DAYS = 7          # 保留天数
HISTORY_INTERVAL = 60     # 采集间隔(秒)

MAX_LOG_LINES = 10000
MAX_EVENTS = 5000

# ─── FTP 默认配置 ────────────────────────────────────────────────────
FTP_CONF = os.path.join(LOG_DIR, "ftp.conf")
FTP_DEFAULT = {
    "enabled": False,
    "host": "192.168.31.5",
    "port": 21,
    "user": "lixin",
    "password": "Tangjin0.",
    "remote_path": "/fnOS/tmp/mint_log",
    "upload_on_rotate": True,
    "keep_local": True,
}

# ─── 爱快路由器 ─────────────────────────────────────────────────────
IKUAI_HOST = "192.168.31.254"
IKUAI_CALL_URL = f"http://{IKUAI_HOST}/Action/call"
IKUAI_USER = "admin"
IKUAI_PASS = "lixin2324"
IKUAI_TOKEN = "ZTVlNGNjZDUtNzkzMy00MzkyLWJkMjEt"

# ─── 线程锁 ──────────────────────────────────────────────────────────
stats_lock = threading.Lock()
sse_lock = threading.Lock()
targets_lock = threading.Lock()

# ─── 全局状态 ────────────────────────────────────────────────────────
log_lines = deque(maxlen=MAX_LOG_LINES)
parsed_events = deque(maxlen=MAX_EVENTS)
sse_clients = []

stats = {
    "total_pings": 0,
    "gw_ok": 0, "v4_ok": 0, "v6_ok": 0,
    "disconnect_events": 0,
    "last_heartbeat": None,
    "gw_rtt": "?", "v4_rtt": "?", "v6_rtt": "?",
    "gw_status": "unknown", "v4_status": "unknown", "v6_status": "unknown",
    "monitor_pid": None, "web_start_time": None,
    "recent_rtt_history": [],
    "success_rate_history": [],
    "bandwidth_rx_kbps": 0, "bandwidth_tx_kbps": 0,
    "tcp_retrans_rate": 0,
    "http_latencies": [],
    "system_stats": [],
    "link_quality": 0,
}

# ─── 逐服务诊断 ─────────────────────────────────────────────────────
DIAG_PATTERN = re.compile(
    r'\[DIAG\]\s+(.+?)\|(.+?)\|(OK|FAIL|DNS_FAIL)\|(.+)'
)
targets = {}
diag_history = {}
DIAG_HISTORY_MAX = 30

# ─── 监控目标配置 ────────────────────────────────────────────────────
MONITOR_CONF = os.path.join(LOG_DIR, "monitor_targets.json")
MONITOR_DEFAULT_TARGETS = {
    "version": 1,
    "gateway": {
        "v4": "192.168.31.254", "v6": "fe80::be24:11ff:fe03:4960", "iface": "ens18"
    },
    "wan": {
        "v4_targets": ["223.5.5.5", "119.29.29.29", "1.1.1.1"],
        "v6_targets": ["2400:3200::1", "240e:ff:e020:99b:0:ff:b099:cff1"],
        "dns_host": "www.baidu.com",
        "http_targets": [
            "https://www.baidu.com",
            "https://www.taobao.com",
            "https://www.jd.com"
        ]
    },
    "lan": {
        "hosts": [
            {"name": "光猫", "ip": "192.168.31.1", "check": "ping"},
            {"name": "爱快路由器", "ip": "192.168.31.254", "check": "ping"},
            {"name": "NAS", "ip": "192.168.31.5", "check": "ping"},
            {"name": "监控服务器", "ip": "192.168.31.251", "check": "ping"}
        ]
    },
    "dns": {
        "resolver": "223.5.5.5",
        "servers": ["192.168.31.254", "192.168.31.251"],
        "domains": [
            {"name": "王者荣耀", "domain": "pvp.qq.com"},
            {"name": "百度", "domain": "www.baidu.com"},
            {"name": "京东", "domain": "www.jd.com"},
            {"name": "腾讯", "domain": "qq.com"},
            {"name": "淘宝", "domain": "www.taobao.com"},
            {"name": "小米", "domain": "www.mi.com"},
            {"name": "华为", "domain": "www.huawei.com"}
        ]
    }
}

# ─── 日志解析正则 ────────────────────────────────────────────────────
LOG_PATTERN = re.compile(
    r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\]\s+\[(\w+\s*)\]\s+(.*)'
)

# ─── 模板目录 ────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(SCRIPT_DIR, "templates")


# ─── SSE 广播 ────────────────────────────────────────────────────────

def broadcast_sse(data):
    """将数据广播到所有 SSE 客户端"""
    msg = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    dead = []
    with sse_lock:
        for q in sse_clients:
            try:
                q.append(msg)
                if len(q) > 500:
                    dead.append(q)
            except Exception:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)
