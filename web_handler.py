"""
web_handler.py — HTTP 请求处理器 (MonitorHandler)
包含所有路由处理逻辑、文件上传、SSE 推送
"""
import glob
import http.server
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse

from config import (
    LOG_DIR, LOG_FILE, MONITOR_LOG, MONITOR_PID_FILE,
    WEBHOOK_CONF, PORT, SCRIPT_DIR,
    log_lines, parsed_events, stats, stats_lock,
    targets, targets_lock, sse_clients, sse_lock,
    TEMPLATES_DIR,
)
from ikuai_client import (
    ikuai_call, get_ikuai_overview, get_ikuai_wan_status,
    get_cached_wan_status, refresh_wan_cache,
    get_ikuai_sysinfo, get_ikuai_live_conn_cached,
)
from history_recorder import read_history
from ftp_manager import (
    read_ftp_config, write_ftp_config,
    test_ftp_connection, upload_logs_to_ftp,
)
from targets_manager import read_monitor_targets, write_monitor_targets
from traceroute_parser import read_all_traces

# ─── 监控端共享状态文件 (net_monitor.sh 写入, Web 只读) ───────────────
STATE_DIR = os.path.join(LOG_DIR, "state")
HEALTH_STATE = os.path.join(STATE_DIR, "health.json")
DEVICES_STATE = os.path.join(STATE_DIR, "devices.json")
PUBLIC_IP_STATE = os.path.join(STATE_DIR, "public_ip.json")
THRESHOLDS_CONF = os.path.join(LOG_DIR, "thresholds.json")


def _read_json(path, default=None):
    """安全读取 JSON 状态文件, 不存在/损坏时返回 default"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ─── 辅助函数 ────────────────────────────────────────────────────────

def parse_ping_history(log_path=None, max_entries=288, bucket_minutes=5, days=1):
    """
    解析监控日志，提取延迟历史（用于折线图）
    数据来源: [HEARTBEAT] gw=Xms v4=Yms v6=Zms
    按 bucket_minutes 窗口汇总，返回最近 max_entries 条（限制在 days 天内）
    """
    if log_path is None:
        log_path = MONITOR_LOG
    history = {"labels": [], "gw_rtt": [], "v4_rtt": [], "v6_rtt": []}
    try:
        with open(log_path, "r", errors="replace") as f:
            lines = f.readlines()[-8000:]

        cutoff = time.time() - days * 86400
        gw_rtts, v4_rtts, v6_rtts = {}, {}, {}
        time_windows = []

        for line in lines:
            ts_match = re.match(r'^\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})', line)
            if not ts_match:
                continue
            ts_str = ts_match.group(1)
            # 解析时间戳判断是否在范围内
            try:
                t = time.mktime(time.strptime(ts_str, "%Y-%m-%d %H:%M"))
            except Exception:
                continue
            if t < cutoff:
                continue

            h, m = int(ts_str[11:13]), int(ts_str[14:16])
            bucket_m = (m // bucket_minutes) * bucket_minutes
            win_key = f"{ts_str[:10]} {h:02d}:{bucket_m:02d}"

            if win_key not in gw_rtts:
                gw_rtts[win_key], v4_rtts[win_key], v6_rtts[win_key] = [], [], []
                time_windows.append(win_key)

            if "[HEARTBEAT]" in line:
                for key, pat, bucket in [
                    ("gw", r"gw=(\d+\.?\d*)", gw_rtts),
                    ("v4", r"v4=(\d+\.?\d*)", v4_rtts),
                    ("v6", r"v6=(\d+\.?\d*)", v6_rtts),
                ]:
                    m = re.search(pat, line)
                    if m:
                        bucket[win_key].append(float(m.group(1)))

        for wk in time_windows[-max_entries:]:
            history["labels"].append(wk)
            history["gw_rtt"].append(
                round(sum(gw_rtts[wk]) / len(gw_rtts[wk]), 1) if gw_rtts[wk] else None)
            history["v4_rtt"].append(
                round(sum(v4_rtts[wk]) / len(v4_rtts[wk]), 1) if v4_rtts[wk] else None)
            history["v6_rtt"].append(
                round(sum(v6_rtts[wk]) / len(v6_rtts[wk]), 1) if v6_rtts[wk] else None)
    except Exception as e:
        print("[parse_ping_history] error:", e)
    return history


def get_combined_status():
    """
    综合判定网络状态：
    - 爱快 WAN 口状态（权重 60%）
    - ping 结果（权重 40%）
    """
    result = {
        "v4_status": "unknown", "v6_status": "unknown", "gw_status": "unknown",
        "ikuai_online": None, "v4_ping_ok": None, "v6_ping_ok": None,
        "gw_ping_ok": None,
        "confidence": "low",
    }
    try:
        ikuai = get_ikuai_wan_status()
        ikuai_online = ikuai.get("all_online", False)
        result["ikuai_online"] = ikuai_online

        with stats_lock:
            total = stats.get("total_pings", 0)
            v4_ok = stats.get("v4_ok", 0)
            v6_ok = stats.get("v6_ok", 0)
            gw_ok = stats.get("gw_ok", 0)
            # 直接取日志解析器维护的状态 (HEARTBEAT + STATS 共同维护)
            stats_v6 = stats.get("v6_status", "unknown")
            stats_v4 = stats.get("v4_status", "unknown")
            stats_gw = stats.get("gw_status", "unknown")
        result["v4_ping_ok"] = round(v4_ok / total * 100, 1) if total > 0 else None
        result["v6_ping_ok"] = round(v6_ok / total * 100, 1) if total > 0 else None
        result["gw_ping_ok"] = round(gw_ok / total * 100, 1) if total > 0 else None

        if ikuai.get("error"):
            # 爱快 API 不可用，依赖 ping 统计
            result["v4_status"] = "up" if (v4_ok > total * 0.5) else stats_v4 if stats_v4 != "unknown" else "down"
            result["v6_status"] = stats_v6 if stats_v6 != "unknown" else ("up" if v6_ok > total * 0.5 else "down")
            result["gw_status"] = "up" if (gw_ok > total * 0.5) else stats_gw if stats_gw != "unknown" else "down"
            result["confidence"] = "medium"
        else:
            if ikuai_online:
                result["v4_status"] = "up"
                result["gw_status"] = "up"
                result["confidence"] = "high"
            else:
                if v4_ok > total * 0.3:
                    result["v4_status"] = "degraded"
                else:
                    result["v4_status"] = stats_v4 if stats_v4 != "unknown" else "down"
                result["gw_status"] = "down" if not (gw_ok > total * 0.3) else "degraded"
                result["confidence"] = "high"
            # v6 状态: 爱快 WAN 不区分 v4/v6，直接用日志解析器的状态
            result["v6_status"] = stats_v6
    except Exception as e:
        result["error"] = str(e)
    return result


# ─── 加载内嵌 HTML 模板 ─────────────────────────────────────────────

def _load_template(name):
    """从 templates/ 目录加载 HTML 模板（每次从磁盘读取，支持热更新）"""
    path = os.path.join(TEMPLATES_DIR, name)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return ""


# ─── HTTP 处理器 ─────────────────────────────────────────────────────

class MonitorHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html, status=200):
        body = html.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    # ─── 页面路由 ────────────────────────────────────────────────

    def _serve_dashboard(self):
        # 优先从磁盘读取 (支持热更新)
        dashboard_file = '/opt/net_monitor/dashboard.html'
        try:
            with open(dashboard_file, 'r', encoding='utf-8') as f:
                self.send_html(f.read())
            return
        except FileNotFoundError:
            pass
        self.send_html(_load_template("dashboard_v2.html"))

    def _serve_ikuai(self):
        ikuai_file = '/opt/net_monitor/ikuai.html'
        try:
            with open(ikuai_file, 'r', encoding='utf-8') as f:
                self.send_html(f.read())
            return
        except Exception:
            pass
        self.send_html(_load_template("ikuai_embed.html"))

    def _serve_webhook(self):
        self.send_html(_load_template("webhook.html"))

    def _serve_ftp(self):
        try:
            with open(os.path.join(SCRIPT_DIR, 'ftp_config.html'), 'r', encoding='utf-8') as f:
                self.send_html(f.read())
        except Exception as e:
            self.send_html("<h2>FTP 配置页面未找到</h2><p>%s</p>" % str(e))

    def _serve_devices(self):
        try:
            with open(os.path.join(SCRIPT_DIR, 'devices.html'), 'r', encoding='utf-8') as f:
                self.send_html(f.read())
        except Exception as e:
            self.send_html("<h2>设备页面未找到</h2><p>%s</p>" % str(e))

    def _serve_config(self):
        try:
            with open(os.path.join(SCRIPT_DIR, 'config.html'), 'r', encoding='utf-8') as f:
                self.send_html(f.read())
        except Exception as e:
            self.send_html("<h2>配置页面未找到</h2><p>%s</p>" % str(e))

    def _serve_admin(self):
        try:
            with open(os.path.join(SCRIPT_DIR, 'admin.html'), 'r', encoding='utf-8') as f:
                self.send_html(f.read())
        except Exception as e:
            self.send_html("<h2>管理页面未找到</h2><p>%s</p>" % str(e))

    def _serve_music_config(self):
        try:
            with open(os.path.join(SCRIPT_DIR, 'music_config.html'), 'r', encoding='utf-8') as f:
                self.send_html(f.read())
        except Exception as e:
            self.send_html("<h2>音乐配置页面未找到</h2><p>%s</p>" % str(e))

    def _serve_static(self, path):
        """通用静态文件服务"""
        import mimetypes
        filepath = os.path.join(SCRIPT_DIR, path.lstrip('/'))
        if not os.path.isfile(filepath):
            self.send_error(404)
            return
        mime, _ = mimetypes.guess_type(filepath)
        self.send_response(200)
        self.send_header('Content-Type', mime or 'application/octet-stream')
        self.send_header('Cache-Control', 'public, max-age=3600')
        self.end_headers()
        with open(filepath, 'rb') as f:
            self.wfile.write(f.read())

    # 音乐配置
    MUSIC_CONF_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'music_conf.json')

    def _load_music_conf(self):
        try:
            with open(self.MUSIC_CONF_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}

    def _save_music_conf(self, conf):
        with open(self.MUSIC_CONF_FILE, 'w') as f:
            json.dump(conf, f)

    def _api_music_config(self):
        if self.command == 'POST':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8')
                data = json.loads(body)
                conf = self._load_music_conf()
                conf['playlist_id'] = data.get('playlist_id', '') or ''
                conf['song_chart'] = data.get('song_chart', '') or ''
                self._save_music_conf(conf)
                self.send_json({'ok': True, 'playlist_id': conf['playlist_id'], 'song_chart': conf['song_chart']})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)}, 500)
        else:
            conf = self._load_music_conf()
            self.send_json(conf)

    def _api_music_proxy(self, path, qs):
        """代理音乐API请求，解决CORS跨域"""
        try:
            import urllib.request
            import urllib.parse
            target = 'http://api.xfyun.club' + path.replace('/api/music', '')
            qs_str = urllib.parse.urlencode({k: v[0] for k, v in qs.items()})
            if qs_str:
                target += '?' + qs_str
            req = urllib.request.Request(target, headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://musicplayer.xfyun.club/'
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            self.send_json({'error': str(e)}, 502)

    # ─── API 路由: 状态与统计 ────────────────────────────────────

    def _api_status(self):
        monitor_running = False
        monitor_pid = None
        try:
            with open(MONITOR_PID_FILE, 'r') as f:
                monitor_pid = int(f.read().strip())
            os.kill(monitor_pid, 0)
            monitor_running = True
        except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError):
            pass

        combined = get_combined_status()
        with stats_lock:
            self.send_json({
                "monitor_running": monitor_running,
                "monitor_pid": monitor_pid,
                "web_pid": os.getpid(),
                "web_start_time": stats["web_start_time"],
                "last_heartbeat": stats["last_heartbeat"],
                "gw_status": combined.get("gw_status", stats["gw_status"]),
                "v4_status": combined.get("v4_status", stats["v4_status"]),
                "v6_status": combined.get("v6_status", stats["v6_status"]),
                "gw_rtt": stats["gw_rtt"],
                "v4_rtt": stats["v4_rtt"],
                "v6_rtt": stats["v6_rtt"],
                "total_pings": stats["total_pings"],
                "gw_ok": stats["gw_ok"],
                "v4_ok": stats["v4_ok"],
                "v6_ok": stats["v6_ok"],
                "disconnect_events": stats["disconnect_events"],
                "log_lines_count": len(log_lines),
                "events_count": len(parsed_events),
                "link_quality": stats["link_quality"],
                "bandwidth_rx_kbps": stats["bandwidth_rx_kbps"],
                "bandwidth_tx_kbps": stats["bandwidth_tx_kbps"],
                "tcp_retrans_rate": stats["tcp_retrans_rate"],
                "http_latencies": stats["http_latencies"][-20:],
                "system_stats": stats["system_stats"][-50:],
                "ikuai_wan": combined.get("ikuai_online"),
                "confidence": combined.get("confidence", "low"),
                "v4_ping_pct": combined.get("v4_ping_ok"),
                "gw_ping_pct": combined.get("gw_ping_ok"),
            })

    def _api_logs(self, qs):
        limit = int(qs.get('limit', [200])[0])
        level = qs.get('level', [None])[0]
        result = list(log_lines)
        if level:
            result = [l for l in result if l['level'] == level]
        self.send_json(result[-limit:])

    def _api_events(self, qs):
        limit = int(qs.get('limit', [100])[0])
        etype = qs.get('type', [None])[0]
        result = list(parsed_events)
        if etype:
            # 按类型筛选: 支持 'disconnect'(断网), 'recover'(恢复), 或精确 event_type
            if etype == 'disconnect':
                result = [e for e in result if 'down' in (e.get('event_type') or '') or 'disconnect' in (e.get('event_type') or '')]
            elif etype == 'recover':
                result = [e for e in result if 'recover' in (e.get('event_type') or '')]
            else:
                result = [e for e in result if e.get('event_type') == etype]
            # 有筛选条件时返回全部匹配项
            self.send_json(result)
            return
        # "全部"模式：混合展示，优先关键事件 + 最近常规事件
        important = [e for e in result if e.get('event_type') in (
            'disconnect', 'v4_down', 'v4_down_v6_alive', 'v6_down', 'inet_down',
            'recover', 'recover_v4', 'recover_v6', 'recover_gw',
            'link_down', 'mass_leave', 'ip_change', 'carrier_change',
            'snapshot', 'no_v4_route', 'conntrack_high',
            'trace_v4_change', 'trace_v6_change', 'arp_change',
            'dns_srv_down', 'http_fail', 'dns_fail',
            'dns_srv_slow'
        )]
        recent = [e for e in result[-limit:] if e not in important]
        combined = important + recent
        # 按时间排序，最多返回 limit * 2
        combined.sort(key=lambda e: e.get('ts', ''))
        self.send_json(combined[-(limit * 2):])

    def _api_disconnects(self):
        types = {
            'disconnect', 'inet_down', 'v4_down', 'v4_down_v6_alive', 'v6_down',
            'recover', 'recover_gw', 'recover_v4', 'recover_v6',
            'link_down', 'mass_leave', 'ip_change', 'v4_ip_change',
            'carrier_change', 'snapshot', 'no_v4_route', 'conntrack_high',
            'trace_v4_change', 'trace_v6_change', 'trace_base',
        }
        result = [e for e in parsed_events if e.get('event_type') in types]
        self.send_json(result[-200:])

    def _api_stats(self):
        with stats_lock:
            self.send_json({
                "success_rate_history": stats["success_rate_history"][-100:],
                "rtt_history": stats["recent_rtt_history"][-100:],
                "disconnect_events": stats["disconnect_events"],
                "total_pings": stats["total_pings"],
                "gw_ok": stats["gw_ok"],
                "v4_ok": stats["v4_ok"],
                "v6_ok": stats["v6_ok"],
                "bandwidth_rx_kbps": stats["bandwidth_rx_kbps"],
                "bandwidth_tx_kbps": stats["bandwidth_tx_kbps"],
                "tcp_retrans_rate": stats["tcp_retrans_rate"],
                "link_quality": stats["link_quality"],
                "http_latencies": stats["http_latencies"][-50:],
                "system_stats": stats["system_stats"][-50:],
            })

    def _api_ping_history(self, qs):
        max_e = int(qs.get('max', [288])[0])
        self.send_json(parse_ping_history(MONITOR_LOG, max_entries=max_e))

    def _api_chart_data(self, qs=None):
        if qs is None:
            qs = {}
        days = int(qs.get('days', [1])[0])
        bucket = int(qs.get('bucket', [5])[0])
        # 限制范围
        days = max(1, min(30, days))
        bucket = max(1, min(360, bucket))

        with stats_lock:
            total = stats.get("total_pings", 0)
            v4_ok = stats.get("v4_ok", 0)
            v6_ok = stats.get("v6_ok", 0)
            gw_ok = stats.get("gw_ok", 0)
        v4_up = round(v4_ok / total * 100, 1) if total > 0 else 0
        v6_up = round(v6_ok / total * 100, 1) if total > 0 else 0
        gw_up = round(gw_ok / total * 100, 1) if total > 0 else 0

        # RTT 直方图 (按协议拆分)
        hist = {"gw": {}, "v4": {}, "v6": {}}
        buckets_def = [(0, 1, "0-1ms"), (1, 2, "1-2ms"), (2, 5, "2-5ms"),
                       (5, 10, "5-10ms"), (10, 20, "10-20ms"), (20, 9999, "20ms+")]
        for _, _, label in buckets_def:
            for proto in ("gw", "v4", "v6"):
                hist[proto][label] = 0
        try:
            with open(MONITOR_LOG, "r", errors="replace") as f:
                lines = f.readlines()[-3000:]
            cutoff = time.time() - days * 86400
            for line in lines:
                if "[HEARTBEAT]" not in line:
                    continue
                ts_match = re.match(r'^\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})', line)
                if ts_match:
                    try:
                        t = time.mktime(time.strptime(ts_match.group(1), "%Y-%m-%d %H:%M"))
                        if t < cutoff: continue
                    except: pass
                for proto, pat in [("gw", r"gw=(\d+\.?\d*)"), ("v4", r"v4=(\d+\.?\d*)"), ("v6", r"v6=(\d+\.?\d*)")]:
                    m = re.search(pat, line)
                    if m:
                        rtt = float(m.group(1))
                        for mn, mx, label in buckets_def:
                            if mn <= rtt < mx:
                                hist[proto][label] += 1
                                break
        except Exception:
            pass
        # 转为前端格式
        hist_labels = [l for _, _, l in buckets_def]
        hist_series = {proto: [hist[proto][l] for l in hist_labels] for proto in ("gw", "v4", "v6")}

        # 爱快数据（用缓存）
        ikuai_data = get_cached_wan_status()
        if not ikuai_data:
            try:
                ikuai_data = refresh_wan_cache()
            except Exception:
                ikuai_data = {"error": "timeout"}

        combined = get_combined_status()
        max_entries = min(int(days * 24 * 60 / bucket), 2000)
        self.send_json({
            "ping_history":   parse_ping_history(MONITOR_LOG, max_entries=max_entries,
                                                  bucket_minutes=bucket, days=days),
            "uptime_pie":     {"labels": ["网关", "IPv4", "IPv6"],
                               "data": [gw_up, v4_up, v6_up],
                               "total_pings": total},
            "rtt_histogram":  {"labels": hist_labels,
                               "datasets": {"gw": hist_series["gw"],
                                            "v4": hist_series["v4"],
                                            "v6": hist_series["v6"]}},
            "ikuai_wan":      ikuai_data,
            "combined":       combined,
        })

    def _api_targets(self):
        with targets_lock:
            self.send_json(dict(targets))

    def _api_traceroute(self):
        self.send_json(read_all_traces())

    # ─── API 路由: Webhook ───────────────────────────────────────

    def _api_webhook_get(self):
        result = {"url": "", "platform": "generic", "secret": "", "enabled": False}
        if os.path.exists(WEBHOOK_CONF):
            try:
                with open(WEBHOOK_CONF, 'r', encoding='utf-8') as f:
                    lines = f.read().splitlines()
                    if len(lines) > 0: result["url"] = lines[0].strip()
                    if len(lines) > 1: result["platform"] = lines[1].strip() or "generic"
                    if len(lines) > 2: result["secret"] = lines[2].strip()
                    if len(lines) > 3: result["enabled"] = lines[3].strip() != "disabled"
            except Exception:
                pass
        self.send_json(result)

    def _api_webhook_post(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            data = json.loads(body)
            url = data.get('url', '').strip()
            platform = data.get('platform', 'generic').strip()
            secret = data.get('secret', '').strip()
            enabled = data.get('enabled', True)
            with open(WEBHOOK_CONF, 'w', encoding='utf-8') as f:
                f.write(url + '\n')
                f.write(platform + '\n')
                f.write(secret + '\n')
                f.write(('enabled' if enabled else 'disabled') + '\n')
            self.send_json({"success": True, "message": "配置已保存"})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)}, 500)

    def _api_webhook_test(self):
        try:
            import hmac
            import hashlib as _hl
            import base64
            import urllib.request as _urllib_req
            import urllib.parse as _urllib_parse
            import time as _time

            url, platform, secret = '', 'generic', ''
            if os.path.exists(WEBHOOK_CONF):
                with open(WEBHOOK_CONF, 'r', encoding='utf-8') as f:
                    cf = f.read().splitlines()
                if len(cf) > 0: url = cf[0].strip()
                if len(cf) > 1: platform = cf[1].strip() or 'generic'
                if len(cf) > 2: secret = cf[2].strip()
            if not url:
                self.send_json({'success': False, 'message': '未配置 Webhook URL，请先保存配置'})
                return
            timestamp = str(int(_time.time() * 1000))
            test_url = url
            if platform == 'dingtalk' and secret:
                sign_str = timestamp + chr(10) + secret
                sign = base64.b64encode(
                    hmac.new(secret.encode('utf-8'), sign_str.encode('utf-8'),
                             _hl.sha256).digest()
                ).decode('utf-8')
                sep = '&' if '?' in url else '?'
                test_url = url + sep + 'timestamp=' + timestamp + '&sign=' + _urllib_parse.quote(sign)
            payload = json.dumps({"msgtype": "text", "text": {"content": "[网络监控测试] 告警通道测试成功！"}})
            req = _urllib_req.Request(test_url, data=payload.encode('utf-8'),
                                      headers={'Content-Type': 'application/json'}, method='POST')
            resp = _urllib_req.urlopen(req, timeout=10)
            resp_data = resp.read().decode('utf-8')
            self.send_json({'success': True, 'message': '测试消息发送成功！' + resp_data[:200]})
        except Exception as e:
            self.send_json({'success': False, 'message': '测试失败: ' + str(e)})

    # ─── API 路由: 爱快路由器 ────────────────────────────────────

    def _api_ikuai_overview(self):
        try:
            self.send_json(get_ikuai_overview())
        except Exception as e:
            print(f"[ikuai] /api/ikuai 异常: {e}")
            self.send_json({"online": False, "error": str(e)})

    def _api_ikuai_call(self, func_name, action="show", param=None):
        try:
            data = ikuai_call(func_name, action, param)
            self.send_json(data if isinstance(data, dict) else {"raw": str(data)})
        except Exception as e:
            self.send_json({"error": str(e)})

    def _api_ikuai_query(self, qs):
        func = qs.get("func", ["status"])[0]
        action = qs.get("action", ["show"])[0]
        param_str = qs.get("param", [""])[0]
        param = json.loads(param_str) if param_str else None
        self._api_ikuai_call(func, action, param)

    def _api_ikuai_dns(self):
        try:
            wan_r = ikuai_call("wan", "show")
            lan_r = ikuai_call("lan", "show")
            self.send_json({
                "wan": wan_r.get("results", {}),
                "lan": lan_r.get("results", {}),
                "raw_wan": wan_r,
                "raw_lan": lan_r,
            })
        except Exception as e:
            self.send_json({"error": str(e)})

    def _api_ikuai_all(self):
        from collections import OrderedDict
        all_data = OrderedDict()
        apis = [
            ("wan", "show", None),
            ("lan", "show", None),
            ("monitor_iface", "show", {"TYPE": "iface_stream"}),
            ("dhcp_lease", "show", None),
            ("dhcp_server", "show", {"TYPE": "data"}),
            ("arp", "show", None),
        ]
        for fn, ac, pm in apis:
            r = ikuai_call(fn, ac, pm)
            all_data[fn] = r
        self.send_json(all_data)

    def _api_ikuai_sysinfo(self):
        """实时路由器 CPU/内存
        补充:
          - live_conn: 来自 overview 的实时 WAN 会话数 (无 monitor_system 的采样滞后)
          - data_stale: monitor_system 采样滞后 > 600s 时为 True (曲线/CPU 卡旧值)
        """
        try:
            data = get_ikuai_sysinfo()
            data["live_conn"] = get_ikuai_live_conn_cached()
            age = data.get("sysinfo_age")
            data["data_stale"] = bool(age is not None and age > 600)
            self.send_json(data)
        except Exception as e:
            self.send_json({"error": str(e)})

    def _api_ikuai_history(self, qs):
        """7天历史趋势 (CPU/内存/延迟)"""
        try:
            days = int(qs.get('days', ['7'])[0])
            bucket = int(qs.get('bucket', ['30'])[0])
            self.send_json(read_history(days=days, bucket_minutes=bucket))
        except Exception as e:
            self.send_json({"error": str(e)})

    # ─── API 路由: FTP ───────────────────────────────────────────

    def _api_ftp_config_get(self):
        cfg = read_ftp_config()
        out = dict(cfg)
        if out.get("password"):
            out["password"] = "********"
        self.send_json(out)

    def _api_ftp_config_post(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            data = json.loads(body)
            old = read_ftp_config()
            new_cfg = dict(old)
            for k in ["enabled", "host", "port", "user", "remote_path",
                       "upload_on_rotate", "keep_local"]:
                if k in data:
                    new_cfg[k] = data[k]
            if "password" in data and data["password"] and data["password"] != "********":
                new_cfg["password"] = data["password"]
            write_ftp_config(new_cfg)
            self.send_json({"success": True, "message": "FTP 配置已保存"})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)}, 500)

    def _api_ftp_test(self):
        cfg = read_ftp_config()
        ok, msg = test_ftp_connection(
            cfg.get("host", ""), cfg.get("port", 21),
            cfg.get("user", ""), cfg.get("password", "")
        )
        self.send_json({"success": ok, "message": msg})

    def _api_ftp_upload(self):
        cfg = read_ftp_config()
        if not cfg.get("enabled"):
            self.send_json({"success": False, "message": "FTP 未启用"})
            return
        results = upload_logs_to_ftp(cfg)
        self.send_json({"success": True, "results": results})

    # ─── API 路由: 监控目标配置 ─────────────────────────────────

    def _api_monitor_targets_get(self):
        cfg = read_monitor_targets()
        self.send_json(cfg)

    def _api_monitor_targets_post(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            data = json.loads(body)
            write_monitor_targets(data)
            self.send_json({"success": True, "message": "监控目标已保存，需重启监控脚本生效"})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)}, 500)

    def _api_monitor_status(self):
        """合并返回监控目标实时状态: LAN + WAN + DNS + 域名诊断"""
        result = {"lan": [], "dns": [], "domains": [], "wan": {}}
        logs = list(log_lines)

        # 1) 内网设备: 从最新 LAN_PING 日志提取
        lan_map = {}
        for l in reversed(logs):
            msg = l.get("msg", "")
            if "LAN_PING" not in msg:
                continue
            m = re.match(r'\[LAN_PING\]\s+(.+?)\|(.+?)\|(OK|FAIL)\|(.+)', msg)
            if not m:
                continue
            name, ip, status, rtt = m.group(1), m.group(2), m.group(3), m.group(4)
            if name not in lan_map:
                lan_map[name] = {"name": name, "ip": ip, "status": status.lower(),
                                 "rtt": rtt.replace("ms",""), "ts": l.get("ts","")}
        # 补充配置中但日志没有的设备
        try:
            targets_cfg = read_monitor_targets()
            for h in targets_cfg.get("lan", {}).get("hosts", []):
                if h["name"] not in lan_map:
                    lan_map[h["name"]] = {"name": h["name"], "ip": h["ip"],
                                          "status": "unknown", "rtt": "?", "ts": ""}
        except Exception:
            pass
        result["lan"] = list(lan_map.values())

        # 2) DNS + 域名诊断 (来自 targets)
        with targets_lock:
            tgt = dict(targets)
        for name, info in tgt.items():
            entry = {"name": name, "ip": info.get("ip","?"),
                     "status": info.get("status","?"),
                     "rtt": str(info.get("rtt","?")).replace("ms",""),
                     "rate": info.get("rate",0)}
            if "DNS" in name:
                result["dns"].append(entry)
            else:
                result["domains"].append(entry)

        # 3) 外网配置摘要
        try:
            cfg = read_monitor_targets()
            result["wan"] = {
                "v4_targets": cfg.get("wan",{}).get("v4_targets",[]),
                "v6_targets": cfg.get("wan",{}).get("v6_targets",[]),
                "http_targets": cfg.get("wan",{}).get("http_targets",[]),
                "dns_host": cfg.get("wan",{}).get("dns_host",""),
            }
        except Exception:
            pass

        self.send_json(result)

    # ─── API 路由: 控制 ──────────────────────────────────────────

    def _api_control_post(self):
        """POST 方式控制服务 (systemctl)"""
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            data = json.loads(body)
            action = data.get('action', '')
            result = {"success": False, "message": "未知操作"}

            action_map = {
                'start_snmpd':   (["systemctl", "start", "snmpd"], "snmpd 已启动"),
                'stop_snmpd':    (["systemctl", "stop", "snmpd"], "snmpd 已停止"),
                'restart_snmpd': (["systemctl", "restart", "snmpd"], "snmpd 已重启"),
                'start_monitor': (["systemctl", "start", "net_monitor"], "监控服务已启动"),
                'stop_monitor':  (["systemctl", "stop", "net_monitor"], "监控服务已停止"),
                'restart_monitor': (["systemctl", "restart", "net_monitor"], "监控服务已重启"),
            }

            if action in action_map:
                cmd, msg = action_map[action]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                result = {"success": r.returncode == 0,
                          "message": msg if r.returncode == 0 else r.stderr}
            else:
                result = {"success": False, "message": "不支持的操作: " + action}
            self.send_json(result)
        except Exception as e:
            self.send_json({"success": False, "message": str(e)}, 500)

    def _api_control_get(self, qs):
        """GET 方式控制监控脚本 (net_monitor_ctl.sh)"""
        action = qs.get('action', ['status'])[0]
        result = {"action": action, "success": False, "message": ""}
        try:
            cmd_map = {
                'restart': ['bash', '/home/li/net_monitor_ctl.sh', 'restart'],
                'stop':    ['bash', '/home/li/net_monitor_ctl.sh', 'stop'],
                'start':   ['bash', '/home/li/net_monitor_ctl.sh', 'start'],
                'web-restart': ['bash', '/home/li/net_monitor_ctl.sh', 'web-restart'],
            }
            if action in cmd_map:
                subprocess.run(cmd_map[action], capture_output=True, timeout=10)
                result["success"] = True
                result["message"] = {"restart": "监控已重启", "stop": "监控已停止", "start": "监控已启动", "web-restart": "Web 已重启"}[action]
            else:
                result["message"] = f"未知操作: {action}"
        except Exception as e:
            result["message"] = str(e)
        self.send_json(result)

    # ─── SSE 流 ──────────────────────────────────────────────────

    def _api_stream(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        q = []
        with sse_lock:
            sse_clients.append(q)

        try:
            with stats_lock:
                init_data = {
                    "type": "init",
                    "gw_status": stats["gw_status"],
                    "v4_status": stats["v4_status"],
                    "v6_status": stats["v6_status"],
                    "gw_rtt": stats["gw_rtt"],
                    "v4_rtt": stats["v4_rtt"],
                    "v6_rtt": stats["v6_rtt"],
                }
            self.wfile.write(f"data: {json.dumps(init_data, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

            while True:
                if q:
                    for msg in q:
                        self.wfile.write(msg.encode('utf-8'))
                        self.wfile.flush()
                    q.clear()
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with sse_lock:
                if q in sse_clients:
                    sse_clients.remove(q)

    # ─── 文件上传 ────────────────────────────────────────────────

    def _handle_file_upload(self):
        """处理 PUT/POST 文件上传"""
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        target = (params.get('path', [''])[0] or '').strip()

        allowed_dirs = ['/opt/net_monitor/', '/home/li/', '/tmp/']
        allowed = any(target.startswith(d) for d in allowed_dirs)
        if not allowed or '..' in target or not target:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"error":"forbidden path"}\n')
            return

        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length > 0 else b''

            if os.path.exists(target):
                backup = target + '.bak'
                shutil.copy2(target, backup)

            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, 'wb') as f:
                f.write(body)

            if target.endswith('.py'):
                pyc_dir = os.path.dirname(target) + '/__pycache__'
                pyc_file = f"{pyc_dir}/{os.path.basename(target).replace('.py','')}.*.pyc"
                for f in glob.glob(pyc_file):
                    try:
                        os.remove(f)
                    except Exception:
                        pass

            backup_exists = os.path.exists(target + '.bak')
            self.send_json({
                "ok": True, "path": target, "size": len(body),
                "backup": target + '.bak' if backup_exists else None,
            })
            print(f"[UPLOAD] 文件已更新: {target} ({len(body)} bytes)")
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f'{{"error":"{str(e)}"}}\n'.encode())

    # ─── API 路由: 健康/设备/公网IP/阈值 ──────────────────────────

    def _healthz(self):
        """外部探活端点 (UptimeRobot 等): 始终 200 + OK"""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def _api_health(self):
        """聚合健康: 监控端子进程存活 + 日志新鲜度 + Web 各线程存活"""
        import threading
        mon = _read_json(HEALTH_STATE, {})
        mpid = mon.get("monitor_pid")
        monitor_alive = False
        if mpid:
            try:
                os.kill(mpid, 0)
                monitor_alive = True
            except OSError:
                monitor_alive = False
        web_threads = {}
        for name in ("history_recorder", "log_watcher", "sys_stats", "ftp_uploader"):
            web_threads[name] = any(
                t.name == name and t.is_alive() for t in threading.enumerate()
            )
        log_age = mon.get("log_age")
        result = {
            "monitor": {
                "pid": mpid,
                "alive": monitor_alive,
                "subprocesses_alive": mon.get("subprocesses_alive"),
                "subprocesses_total": mon.get("subprocesses_total"),
                "log_age": log_age,
            },
            "web_threads": web_threads,
            "ok": monitor_alive and all(web_threads.values())
                  and (log_age is None or log_age < 600),
            "ts": int(time.time()),
        }
        self.send_json(result)

    def _api_devices(self):
        """设备在线/离线时间线: 合并 presence 状态与 LAN 主机名"""
        devices = _read_json(DEVICES_STATE, {}) or {}
        try:
            cfg = read_monitor_targets()
            name_map = {h["ip"]: h["name"] for h in cfg.get("lan", {}).get("hosts", [])}
        except Exception:
            name_map = {}
        out = []
        for ip, rec in devices.items():
            entry = dict(rec)
            entry["ip"] = ip
            entry["name"] = name_map.get(ip, "")
            out.append(entry)
        out.sort(key=lambda e: (e.get("status") != "online", -e.get("last_seen", 0)))
        self.send_json({"devices": out, "count": len(out)})

    def _api_public_ip(self):
        """当前公网 IPv4 及 CGNAT 判定"""
        self.send_json(_read_json(PUBLIC_IP_STATE, {}))

    def _api_thresholds(self):
        """读取当前阈值 (默认值或被 thresholds.json 覆盖)"""
        defaults = {
            "TH_CPU_WARN": 80, "TH_CPU_CRIT": 90,
            "TH_MEM_WARN": 85, "TH_MEM_CRIT": 95,
            "TH_DISK_WARN": 85, "TH_DISK_CRIT": 95,
            "TH_RETRANS_WARN": 2, "TH_RETRANS_CRIT": 5,
            "TH_LOSS_WARN": 5, "TH_LOSS_CRIT": 15,
            "TH_RTT_WARN": 150, "TH_RTT_CRIT": 300,
            "TH_QUALITY_WARN": 70, "TH_QUALITY_CRIT": 50,
            "TH_CONNTRACK_WARN": 70, "TH_CONNTRACK_CRIT": 85,
        }
        cur = _read_json(THRESHOLDS_CONF, {}) or {}
        merged = {k: cur.get(k, v) for k, v in defaults.items()}
        self.send_json({"defaults": defaults, "current": merged,
                        "note": "修改后监控端约 10-30s 内自动生效, 无需重启"})

    def _api_thresholds_post(self):
        """写入阈值 (仅接受 TH_* 数值键)"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body)
        except Exception as e:
            self.send_json({"success": False, "message": str(e)}, 500)
            return
        allowed = {"TH_CPU_WARN", "TH_CPU_CRIT", "TH_MEM_WARN", "TH_MEM_CRIT",
                   "TH_DISK_WARN", "TH_DISK_CRIT", "TH_RETRANS_WARN", "TH_RETRANS_CRIT",
                   "TH_LOSS_WARN", "TH_LOSS_CRIT", "TH_RTT_WARN", "TH_RTT_CRIT",
                   "TH_QUALITY_WARN", "TH_QUALITY_CRIT",
                   "TH_CONNTRACK_WARN", "TH_CONNTRACK_CRIT"}
        new_cfg = {}
        for k, v in data.items():
            if k in allowed and isinstance(v, (int, float)):
                new_cfg[k] = v
        if not new_cfg:
            self.send_json({"success": False, "message": "无有效阈值项"})
            return
        try:
            with open(THRESHOLDS_CONF, "w", encoding="utf-8") as f:
                json.dump(new_cfg, f, ensure_ascii=False, indent=2)
            self.send_json({"success": True,
                            "message": "阈值已保存, 监控端将自动重载",
                            "written": new_cfg})
        except OSError as e:
            self.send_json({"success": False, "message": str(e)}, 500)

    # ─── 路由分发 ────────────────────────────────────────────────

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # 页面路由
        if path in ('/', '/index.html', '/dashboard.html', '/dashboard'):
            return self._serve_dashboard()
        elif path in ('/ikuai.html', '/ikuai'):
            return self._serve_ikuai()
        elif path in ('/webhook.html', '/webhook'):
            return self._serve_webhook()
        elif path in ('/ftp.html', '/ftp', '/ftp_config.html'):
            return self._serve_ftp()
        elif path in ('/config.html', '/config', '/monitor_config.html'):
            return self._serve_config()
        elif path in ('/admin.html', '/admin'):
            return self._serve_admin()
        elif path in ('/music_config.html', '/music'):
            return self._serve_music_config()
        elif path in ('/devices.html', '/devices'):
            return self._serve_devices()

        # 静态文件 (播放器等)
        elif path.startswith('/player/') or path.startswith('/js/') or path.startswith('/css/') or \
             path.startswith('/xf-MusicPlayer/'):
            return self._serve_static(path)
        elif path.endswith('.js') or path.endswith('.css') or path.endswith('.woff') or path.endswith('.woff2'):
            return self._serve_static(path)

        # 音乐配置API
        elif path == '/api/music/config':
            return self._api_music_config()

        # 音乐API代理 (解决CORS)
        elif path.startswith('/api/music'):
            return self._api_music_proxy(path, qs)

        # API 路由
        elif path == '/api/status':
            return self._api_status()
        elif path == '/api/logs':
            return self._api_logs(qs)
        elif path == '/api/events':
            return self._api_events(qs)
        elif path == '/api/disconnects':
            return self._api_disconnects()
        elif path == '/api/stats':
            return self._api_stats()
        elif path == '/api/ping-history':
            return self._api_ping_history(qs)
        elif path == '/api/ikuai-wan':
            return self.send_json(get_ikuai_wan_status())
        elif path == '/api/chart-data':
            return self._api_chart_data(qs)
        elif path == '/api/targets':
            return self._api_targets()
        elif path == '/api/traceroute':
            return self._api_traceroute()
        elif path == '/api/webhook':
            return self._api_webhook_get()
        elif path == '/api/webhook-test':
            return self._api_webhook_test()

        # 爱快 API
        elif path == '/api/ikuai':
            return self._api_ikuai_overview()
        elif path == '/api/ikuai/status':
            return self._api_ikuai_call("status", "show")
        elif path == '/api/ikuai/interfaces':
            return self._api_ikuai_call("network_interface_eth", "show",
                                        {"TYPE": "data,total", "limit": "0,50"})
        elif path == '/api/ikuai/clients':
            return self._api_ikuai_call("ip_monitor", "show",
                                        {"TYPE": "data,total", "limit": "0,100"})
        elif path.startswith('/api/ikuai/query'):
            return self._api_ikuai_query(qs)
        elif path == '/api/ikuai/terminals':
            return self._api_ikuai_call("dhcp_lease", "show")
        elif path == '/api/ikuai/dhcp':
            return self._api_ikuai_call("dhcp_server", "show", {"TYPE": "data"})
        elif path == '/api/ikuai/arp':
            return self._api_ikuai_call("arp", "show")
        elif path == '/api/ikuai/dns':
            return self._api_ikuai_dns()
        elif path == '/api/ikuai/all':
            return self._api_ikuai_all()
        elif path == '/api/ikuai/sysinfo':
            return self._api_ikuai_sysinfo()
        elif path == '/api/ikuai/history':
            return self._api_ikuai_history(qs)

        # FTP API
        elif path == '/api/ftp_config':
            return self._api_ftp_config_get()
        elif path == '/api/ftp_test':
            return self._api_ftp_test()
        elif path == '/api/ftp_upload':
            return self._api_ftp_upload()

        # 监控目标配置 API
        elif path == '/api/monitor_targets':
            return self._api_monitor_targets_get()
        elif path == '/api/monitor_status':
            return self._api_monitor_status()

        # 控制 API
        elif path == '/api/control':
            return self._api_control_get(qs)

        # 健康/设备/公网IP/阈值 API
        elif path == '/healthz':
            return self._healthz()
        elif path == '/api/health':
            return self._api_health()
        elif path == '/api/devices':
            return self._api_devices()
        elif path == '/api/public_ip':
            return self._api_public_ip()
        elif path == '/api/thresholds':
            return self._api_thresholds()

        # SSE
        elif path == '/api/stream':
            return self._api_stream()

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 文件上传
        if path == '/api/file':
            return self._handle_file_upload()

        # POST 方式的 API
        elif path == '/api/webhook':
            return self._api_webhook_post()
        elif path == '/api/ftp_config':
            return self._api_ftp_config_post()
        elif path == '/api/thresholds':
            return self._api_thresholds_post()
        elif path == '/api/monitor_targets':
            return self._api_monitor_targets_post()
        elif path == '/api/control':
            return self._api_control_post()

        # 其他 POST 路由走 GET 逻辑（兼容原 do_POST = do_GET）
        else:
            return self.do_GET()

    def do_PUT(self):
        """PUT 文件上传"""
        self._handle_file_upload()
