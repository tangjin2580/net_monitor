"""
system_stats.py — 系统资源采集线程 (CPU/内存/磁盘/负载/带宽)
"""
import subprocess
import sys
import time

from config import stats, stats_lock


def system_stats_collector():
    """后台线程: 每10秒采集一次系统资源，直接更新 stats"""
    while True:
        try:
            # CPU 使用率 (从 /proc/stat 两次采样)
            cpu_pct = 0
            try:
                with open('/proc/stat') as f:
                    fields1 = f.readline().split()
                time.sleep(0.5)
                with open('/proc/stat') as f:
                    fields2 = f.readline().split()
                v1 = [int(x) for x in fields1[1:9]]
                v2 = [int(x) for x in fields2[1:9]]
                t1, i1 = sum(v1), v1[3] + v1[4]
                t2, i2 = sum(v2), v2[3] + v2[4]
                total_diff = t2 - t1
                idle_diff = i2 - i1
                if total_diff > 0:
                    cpu_pct = max(0, min(100, int((total_diff - idle_diff) * 100 / total_diff)))
            except Exception:
                pass

            # 内存使用率
            mem_pct = 0
            try:
                mi = {}
                with open('/proc/meminfo') as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 2:
                            mi[parts[0].rstrip(':')] = int(parts[1])
                total = mi.get('MemTotal', 0)
                avail = mi.get('MemAvailable', mi.get('MemFree', 0))
                if total > 0:
                    mem_pct = int((total - avail) * 100 / total)
            except Exception:
                pass

            # 磁盘使用率
            disk_pct = 0
            try:
                out = subprocess.check_output(['df', '/'], text=True).splitlines()
                if len(out) >= 2:
                    disk_pct = int(out[1].split()[-2].rstrip('%'))
            except Exception:
                pass

            # 负载
            load1 = 0.0
            try:
                with open('/proc/loadavg') as f:
                    load1 = float(f.readline().split()[0])
            except Exception:
                pass

            # 带宽：读取当前累计字节数
            rx_bytes = tx_bytes = 0
            try:
                iface = 'eth0'
                try:
                    routes = subprocess.check_output(
                        ['ip', 'route', 'show', 'default'], text=True)
                    for rline in routes.splitlines():
                        if 'dev ' in rline:
                            iface = rline.split('dev ')[1].split()[0]
                            break
                except Exception:
                    pass
                with open('/proc/net/dev') as f:
                    for line in f:
                        if ':' in line and iface in line:
                            parts = line.split(':')[1].split()
                            rx_bytes = int(parts[0])
                            tx_bytes = int(parts[8])
                            break
            except Exception:
                pass
            now = time.time()

            # 更新 stats
            with stats_lock:
                entry = {
                    "ts": time.strftime("%H:%M:%S"),
                    "cpu_pct": cpu_pct,
                    "mem_pct": mem_pct,
                    "disk_pct": disk_pct,
                    "load1": load1,
                }
                stats["system_stats"].append(entry)
                if len(stats["system_stats"]) > 100:
                    stats["system_stats"] = stats["system_stats"][-100:]

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
                print(
                    f"[sys_stats] cpu={cpu_pct}% mem={mem_pct}% disk={disk_pct}% "
                    f"load={load1:.2f} rx={stats['bandwidth_rx_kbps']}kbps "
                    f"tx={stats['bandwidth_tx_kbps']}kbps",
                    file=sys.stderr,
                )

            time.sleep(10)
        except Exception:
            time.sleep(10)
