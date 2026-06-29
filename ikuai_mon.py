#!/usr/bin/env python3
"""
ikuai_mon.py — 爱快路由器监控脚本（纯标准库，增强版 v2）
调爱快 API，获取 WAN 状态、流量、终端列表(DHCP租约)、ARP、DNS 等
用法:
  python3 ikuai_mon.py              # 输出当前完整状态 JSON
  python3 ikuai_mon.py --monitor    # 持续监控（每15秒）
  python3 ikuai_mon.py --check      # 快速检查（0=在线/1=离线）
  python3 ikuai_mon.py --terminals  # 输出在线终端列表 JSON
  python3 ikuai_mon.py --arp        # 输出 ARP 表 JSON
  python3 ikuai_mon.py --all        # 输出所有数据 JSON
"""
import json, hashlib, time, sys, argparse, urllib.request, urllib.error
from http.cookiejar import CookieJar, Cookie
from datetime import datetime, timezone, timedelta

IKUAI_HOST     = "192.168.31.254"
IKUAI_USER     = "admin"
IKUAI_PASS     = "lixin2324"
IKUAI_LOGIN    = f"http://{IKUAI_HOST}/Action/login"
IKUAI_API      = f"http://{IKUAI_HOST}/Action/call"

# 全局 cookie jar
_jar        = None
_last_login = 0

def ensure_login():
    global _jar, _last_login
    now = time.time()
    if _jar and (_last_login > 0) and (now - _last_login < 300):
        return True
    _jar = CookieJar()
    passwd_md5 = hashlib.md5(IKUAI_PASS.encode()).hexdigest()
    body = json.dumps({
        "username": IKUAI_USER, "passwd": passwd_md5,
        "pass": "", "remember_password": True
    }).encode("utf-8")
    handler = urllib.request.HTTPCookieProcessor(_jar)
    opener  = urllib.request.build_opener(handler)
    req    = urllib.request.Request(
        IKUAI_LOGIN, data=body,
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with opener.open(req, timeout=10) as resp:
            d = json.loads(resp.read())
            if d.get("code") != 0:
                return False
        for name, val in [("username", IKUAI_USER), ("login", "1")]:
            _jar.set_cookie(Cookie(
                0, name, val,
                None, False,
                IKUAI_HOST, True, True, "/", True,
                False, None, False, None, None, {}
            ))
        _last_login = now
        return True
    except Exception:
        return False

def ikuai_call(func_name, action="show", param=None):
    if not ensure_login():
        return {"error": "登录失败"}
    passwd_md5 = hashlib.md5(IKUAI_PASS.encode()).hexdigest()
    body = {"username": IKUAI_USER, "passwd": passwd_md5,
            "func_name": func_name, "action": action}
    if param:
        body["param"] = param
    handler = urllib.request.HTTPCookieProcessor(_jar)
    opener  = urllib.request.build_opener(handler)
    req    = urllib.request.Request(
        IKUAI_API, data=json.dumps(body).encode("utf-8"),
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json",
            "isAjax": "1",
            "Origin": f"http://{IKUAI_HOST}",
            "Referer": f"http://{IKUAI_HOST}/",
        },
        method="POST"
    )
    try:
        with opener.open(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

# ─── 时间转换辅助 ────────────────────────────────────────────────────────────

def _ts_to_str(ts):
    """将爱快时间戳（秒）转为可读字符串"""
    try:
        ts_int = int(ts)
        # 爱快时间戳可能是从2020-01-01起的秒数，或直接是Unix时间戳
        # 尝试判断：如果 > 1e10 则是毫秒，如果在 1.5e9~2e9 之间则是Unix时间戳
        if ts_int > 1_000_000_000_000:
            ts_int //= 1000
        # 尝试作为 Unix 时间戳
        dt = datetime.fromtimestamp(ts_int, tz=timezone(timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)

# ─── 数据获取函数 ───────────────────────────────────────────────────────────

def get_wan_status():
    """获取 WAN 口状态（含 DNS 信息）"""
    result = {"wan": [], "online": False, "dns_servers": []}
    r = ikuai_call("wan", "show")
    if r.get("code") == 0 and r.get("results"):
        for w in r["results"].get("data", []):
            dns_str = w.get("dns", "")
            dns_list = [s.strip() for s in dns_str.split(",") if s.strip()] if dns_str else []
            info = {
                "name":           w.get("name", w.get("tagname", "?")),
                "id":             w.get("id", 0),
                "pppoe_status":   w.get("pppoe_status", 0),
                "pppoe_status_s": _wan_status_str(w.get("pppoe_status", 0)),
                "internet":       w.get("internet", 0),
                "internet_s":     _internet_str(w.get("internet", 0)),
                "ip_addr":        w.get("pppoe_ip_addr", ""),
                "gw":             w.get("gateway", ""),
                "dns":            dns_list,
                "dns_raw":        dns_str,
                "uptime":         w.get("uptime", 0),
                "check_host":     w.get("check_link_host", ""),
                "mtu":            w.get("mtu", 0),
                "netmask":        w.get("netmask", ""),
                "mac":            w.get("mac", ""),
            }
            result["wan"].append(info)
            if w.get("pppoe_status") == 2 or w.get("internet") == 2:
                result["online"] = True
            if dns_list:
                result["dns_servers"].extend(dns_list)
    else:
        result["error"] = f"wan API: {r.get('message','?')}"
    return result

def get_iface_traffic():
    """获取各接口实时流量"""
    result = {"iface": []}
    r = ikuai_call("monitor_iface", "show", {"TYPE": "iface_stream"})
    if r.get("code") == 0 and r.get("results"):
        for i in r["results"].get("iface_stream", []):
            result["iface"].append({
                "interface":   i.get("interface", "?"),
                "upload":      i.get("upload", 0),
                "download":    i.get("download", 0),
                "total_up":    i.get("total_up", 0),
                "total_down":  i.get("total_down", 0),
                "up_dropped":  i.get("updropped", 0),
                "down_dropped": i.get("downdropped", 0),
            })
    return result

def get_lan_info():
    """获取 LAN 口信息"""
    result = {"lan": []}
    r = ikuai_call("lan", "show")
    if r.get("code") == 0 and r.get("results"):
        for l in r["results"].get("data", []):
            result["lan"].append({
                "name":      l.get("name", l.get("tagname", "?")),
                "ip_addr":   l.get("ip_addr", ""),
                "netmask":   l.get("netmask", ""),
                "dhcp_en":   l.get("dhcpd_enable", False),
                "dhcp_start": l.get("dhcpd_start", ""),
                "dhcp_end":  l.get("dhcpd_end", ""),
                "lease_time": l.get("dhcpd_leasetime", 0),
                "dns1":      l.get("dns1", ""),
                "dns2":      l.get("dns2", ""),
            })
    return result

def get_dhcp_leases():
    """获取 DHCP 租约列表（在线终端，使用 dhcp_lease API）"""
    result = {"leases": [], "total": 0, "static_total": 0}
    r = ikuai_call("dhcp_lease", "show")
    if r.get("code") == 0 and r.get("results"):
        leases = r["results"].get("data", [])
        for lease in leases:
            result["leases"].append({
                "id":           lease.get("id", 0),
                "ip":           lease.get("ip_addr", ""),
                "mac":          lease.get("mac", ""),
                "termname":     lease.get("termname", ""),
                "hostname":     lease.get("hostname", ""),
                "interface":    lease.get("interface", ""),
                "static":       lease.get("static_status", 0) == 1,
                "active":       lease.get("status", 0) == 1,
                "start_time_s": _ts_to_str(lease.get("start_time", 0)),
                "end_time_s":   _ts_to_str(lease.get("end_time", 0)),
                "timeout":      lease.get("timeout", 0),
            })
            if lease.get("static_status", 0) == 1:
                result["static_total"] += 1
        result["total"] = len(result["leases"])
    else:
        result["error"] = r.get("message", "DHCP lease API 调用失败")
    return result

def get_arp_table():
    """获取 ARP 表"""
    result = {"arp": [], "total": 0}
    r = ikuai_call("arp", "show")
    if r.get("code") == 0 and r.get("results"):
        arp_data = r["results"].get("data", [])
        for a in arp_data:
            result["arp"].append({
                "id":         a.get("id", 0),
                "ip":         a.get("ip_addr", a.get("addr", "")),
                "mac":        a.get("mac", a.get("hwaddr", "")),
                "interface":  a.get("interface", ""),
                "tagname":    a.get("tagname", ""),
                "bind_type":  a.get("bind_type", "0"),
                "bind_state": a.get("bind_state", "0"),
            })
        result["total"] = len(result["arp"])
    return result

def get_dns_config():
    """获取 DNS 配置（从 WAN 信息中提取，因为 dns_forward API 被拒）"""
    result = {"wan_dns": [], "dhcp_dns": [], "error": None}
    wan_r = get_wan_status()
    for w in wan_r.get("wan", []):
        if w.get("dns"):
            result["wan_dns"].append({
                "wan":   w.get("name", "?"),
                "dns":   w.get("dns", []),
                "dns_raw": w.get("dns_raw", ""),
            })
    lan_r = get_lan_info()
    for l in lan_r.get("lan", []):
        dns_list = [d for d in [l.get("dns1", ""), l.get("dns2", "")] if d]
        if dns_list:
            result["dhcp_dns"].append({
                "lan":    l.get("name", "?"),
                "dns":    dns_list,
                "dns1":   l.get("dns1", ""),
                "dns2":   l.get("dns2", ""),
            })
    return result

def get_terminal_list():
    """获取在线终端列表（合并 DHCP 租约 + ARP）"""
    result = {"terminals": [], "total": 0, "by_interface": {}, "source": "dhcp_lease+arp"}

    # 主数据来源：DHCP 租约
    leases = get_dhcp_leases()
    terminal_map = {}

    for lease in leases.get("leases", []):
        ip = lease.get("ip", "")
        if ip:
            terminal_map[ip] = {
                "ip":         ip,
                "mac":        lease.get("mac", ""),
                "name":       lease.get("termname") or lease.get("hostname") or "",
                "hostname":   lease.get("hostname", ""),
                "termname":   lease.get("termname", ""),
                "interface":  lease.get("interface", ""),
                "static":     lease.get("static", False),
                "active":     lease.get("active", False),
                "lease_expire": lease.get("end_time_s", ""),
                "source":     "dhcp_lease",
            }

    # 补充：ARP 表中有但 DHCP 中没有的设备
    arp = get_arp_table()
    for a in arp.get("arp", []):
        ip = a.get("ip", "")
        if ip and ip not in terminal_map:
            terminal_map[ip] = {
                "ip":         ip,
                "mac":        a.get("mac", ""),
                "name":       a.get("tagname", ""),
                "hostname":   "",
                "termname":   a.get("tagname", ""),
                "interface":  a.get("interface", ""),
                "static":     False,
                "active":     True,
                "lease_expire": "",
                "source":     "arp",
            }

    # 按接口分组
    by_iface = {}
    for t in terminal_map.values():
        iface = t.get("interface", "unknown")
        if iface not in by_iface:
            by_iface[iface] = 0
        by_iface[iface] += 1

    result["terminals"] = list(terminal_map.values())
    result["total"] = len(result["terminals"])
    result["by_interface"] = by_iface
    return result

def get_dhcp_server_config():
    """获取 DHCP 服务器配置"""
    result = {"configs": []}
    r = ikuai_call("dhcp_server", "show")
    if r.get("code") == 0 and r.get("results"):
        for c in r["results"].get("data", []):
            result["configs"].append({
                "name":       c.get("tagname", "?"),
                "interface":  c.get("interface", ""),
                "addr_pool":  c.get("addr_pool", ""),
                "netmask":    c.get("netmask", ""),
                "gateway":    c.get("gateway", ""),
                "dns1":       c.get("dns1", ""),
                "dns2":       c.get("dns2", ""),
                "lease":      c.get("lease", 0),
                "enabled":    c.get("enabled", "no"),
                "available":  c.get("available", 0),
            })
    return result

def get_all_status():
    """获取所有状态，返回完整 dict"""
    result = {
        "timestamp":   int(time.time()),
        "datetime":    time.strftime("%Y-%m-%d %H:%M:%S"),
        "online":      False,
        "wan":         {},
        "traffic":     {},
        "lan":         {},
        "terminals":   {},
        "dhcp":        {},
        "dhcp_config": {},
        "arp":         {},
        "dns":         {},
        "errors":      [],
    }
    try:
        wan = get_wan_status()
        result["wan"] = wan
        result["online"] = wan.get("online", False)
    except Exception as e:
        result["errors"].append(f"wan: {e}")

    try:
        result["traffic"] = get_iface_traffic()
    except Exception as e:
        result["errors"].append(f"traffic: {e}")

    try:
        result["lan"] = get_lan_info()
    except Exception as e:
        result["errors"].append(f"lan: {e}")

    try:
        result["terminals"] = get_terminal_list()
    except Exception as e:
        result["errors"].append(f"terminals: {e}")

    try:
        result["dhcp"] = get_dhcp_leases()
    except Exception as e:
        result["errors"].append(f"dhcp: {e}")

    try:
        result["dhcp_config"] = get_dhcp_server_config()
    except Exception as e:
        result["errors"].append(f"dhcp_config: {e}")

    try:
        result["arp"] = get_arp_table()
    except Exception as e:
        result["errors"].append(f"arp: {e}")

    try:
        result["dns"] = get_dns_config()
    except Exception as e:
        result["errors"].append(f"dns: {e}")

    return result

# ─── 辅助函数 ────────────────────────────────────────────────────────────────

def _wan_status_str(code):
    m = {0: "断开", 1: "拨号中", 2: "已连接", 3: "失败", 4: "正在断开"}
    return m.get(code, f"未知({code})")

def _internet_str(code):
    m = {0: "未检测", 1: "不通", 2: "通畅"}
    return m.get(code, f"未知({code})")

# ─── 命令行入口 ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="爱快路由器监控脚本 v2")
    parser.add_argument("--monitor",   action="store_true", help="持续监控模式（每15秒）")
    parser.add_argument("--check",     action="store_true", help="快速检查（0=在线/1=离线）")
    parser.add_argument("--terminals", action="store_true", help="输出在线终端列表 JSON")
    parser.add_argument("--arp",       action="store_true", help="输出 ARP 表 JSON")
    parser.add_argument("--dns",       action="store_true", help="输出 DNS 配置 JSON")
    parser.add_argument("--dhcp",      action="store_true", help="输出 DHCP 服务器配置 JSON")
    parser.add_argument("--traffic",   action="store_true", help="输出流量信息 JSON")
    parser.add_argument("--all",       action="store_true", help="输出所有数据 JSON")
    args = parser.parse_args()

    if args.monitor:
        print(f"[{time.strftime('%H:%M:%S')}] 爱快监控启动", flush=True)
        while True:
            try:
                s = get_wan_status()
                ts = time.strftime("%H:%M:%S")
                # ── 结构化日志（供 ikuai_wan_monitor 解析）──
                print(f"[IKUAI_STATUS] online={str(s.get('online', False)).lower()}")
                if s.get("online"):
                    for w in s["wan"]:
                        status_str = _wan_status_str(w.get("pppoe_status", 0))
                        internet_str = "可达" if w.get("internet", 0) == 2 else "不可达"
                        print(f"[IKUAI_WAN] {w.get('name','?')}: status={w.get('pppoe_status',0)} internet={w.get('internet',0)} pppoe_ip={w.get('pppoe_ip_addr','')} proto={w.get('wan_proto','?')} | {status_str} | 互联网:{internet_str}")
                else:
                    print(f"[IKUAI_STATUS] error={s.get('error','?')}")
                # ── 人类可读摘要 ──
                if s.get("online"):
                    wan_str = " | ".join(
                        f"{w['name']}:{_wan_status_str(w['pppoe_status'])}"
                        for w in s["wan"]
                    )
                    traffic = get_iface_traffic()
                    iface_str = " | ".join(
                        f"{i['interface']} ↓{i['download']/1000000:.1f} ↑{i['upload']/1000000:.1f} Mbps"
                        for i in traffic.get("iface", [])
                        if i.get("download", 0) > 0 or i.get("upload", 0) > 0
                    ) if traffic.get("iface") else ""
                    terminals = get_terminal_list()
                    term_str = f"终端:{terminals['total']}台"
                    print(f"[{ts}] ✅ {wan_str} | {term_str} | {iface_str}", flush=True)
                else:
                    print(f"[{ts}] ❌ 离线 | {s.get('error','?')}", flush=True)
            except KeyboardInterrupt:
                print("\n停止", flush=True)
                break
            except Exception as e:
                print(f"[{ts}] 错误: {e}", flush=True)
            time.sleep(15)

    elif args.check:
        s = get_wan_status()
        sys.exit(0 if s.get("online") else 1)

    elif args.terminals:
        print(json.dumps(get_terminal_list(), ensure_ascii=False, indent=2))
    elif args.arp:
        print(json.dumps(get_arp_table(), ensure_ascii=False, indent=2))
    elif args.dns:
        print(json.dumps(get_dns_config(), ensure_ascii=False, indent=2))
    elif args.dhcp:
        print(json.dumps(get_dhcp_server_config(), ensure_ascii=False, indent=2))
    elif args.traffic:
        print(json.dumps(get_iface_traffic(), ensure_ascii=False, indent=2))
    elif args.all:
        print(json.dumps(get_all_status(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(get_all_status(), ensure_ascii=False, indent=2))
