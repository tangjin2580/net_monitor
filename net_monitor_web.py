#!/usr/bin/env python3
"""
net_monitor_web.py — 网络监控 Web 仪表盘入口
端口: 9091
用法: nohup python3 /home/li/net_monitor_web.py &
停止: kill $(cat /home/li/net_monitor_web.pid)

模块拆分:
  config.py            — 配置常量 + 全局状态
  log_parser.py        — 日志解析与事件分类
  system_stats.py      — 系统资源采集线程
  ikuai_client.py      — 爱快路由器 API 客户端
  ftp_manager.py       — FTP 日志归档
  traceroute_parser.py — Traceroute 文件解析
  web_handler.py       — HTTP 请求处理器
  templates/           — 内嵌 HTML 模板
"""
import http.server
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Any

# ─── 从拆分模块导入 ──────────────────────────────────────────────────
from config import (
    PORT, PID_FILE, LOG_FILE,
    log_lines, parsed_events, stats, init_logging,
)

from log_parser import process_line, log_watcher
from system_stats import system_stats_collector
from history_recorder import history_recorder
from ftp_scheduler import ftp_daily_uploader
from web_handler import MonitorHandler

# ─── 兼容 shim: 旧代码可能直接 import 这些名字 ──────────────────────
from config import *                          # noqa: F401,F403
from log_parser import *                      # noqa: F401,F403
from ikuai_client import *                    # noqa: F401,F403
from ftp_manager import *                     # noqa: F401,F403
from traceroute_parser import *               # noqa: F401,F403
from web_handler import (                     # noqa: F401
    parse_ping_history, get_combined_status,
)

# 注意: 必须放在 `from xxx import *` 兼容 shim 之后, 否则会被其它模块的
# 同名 `logger` 通配导入覆盖, 导致本模块的日志前缀变成别的模块名。
logger = logging.getLogger("net_monitor_web")


# ─── 主入口 ──────────────────────────────────────────────────────────
def _pid_alive_and_ours(pid: int) -> bool:
    """进程是否存活且确实是 net_monitor_web (防止 PID 复用误杀)"""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            cmd = f.read().decode("utf-8", "replace")
        if "net_monitor_web" not in cmd:
            return False
    except (IOError, OSError):
        pass  # 读不到则信任 os.kill 的结果
    return True


def main() -> None:
    # 统一日志配置 (仅首个调用生效, 其余调用被 force 覆盖或忽略)
    init_logging()

    # 单例保护: 启动前若已有活着的旧 web 实例, 先终止之。
    # 否则重复实例会各自启动 history_recorder 线程, 重复打 iKuai API,
    # 把路由器 NAT 会话连接数放大约 N 倍 (曾导致爱快 CPU 飙高)。
    try:
        with open(PID_FILE) as f:
            _old = int(f.read().strip())
    except (OSError, ValueError):
        _old = None
    if _old and _old != os.getpid() and _pid_alive_and_ours(_old):
        try:
            os.kill(_old, signal.SIGTERM)
            logger.info("终止旧 web 实例 PID %s", _old)
        except OSError:
            pass
        time.sleep(1)
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    stats["web_start_time"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 加载历史日志
    if os.path.exists(LOG_FILE):
        logger.info("加载历史日志: %s", LOG_FILE)
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                process_line(line)
        logger.info("已加载 %d 行日志, %d 个事件", len(log_lines), len(parsed_events))

    # 启动系统资源采集线程
    t_sys = threading.Thread(target=system_stats_collector, daemon=True, name="sys_stats")
    t_sys.start()
    logger.info("[THREAD] system_stats_collector 已启动")

    # 启动历史趋势记录线程 (CPU/内存/延迟, 7天)
    t_hist = threading.Thread(target=history_recorder, daemon=True, name="history_recorder")
    t_hist.start()
    logger.info("[THREAD] history_recorder 已启动")

    # 启动 FTP 每日定时上传线程 (日志归档 + 趋势数据)
    t_ftp = threading.Thread(target=ftp_daily_uploader, daemon=True, name="ftp_uploader")
    t_ftp.start()
    logger.info("[THREAD] ftp_daily_uploader 已启动")

    # 启动日志监控线程
    watcher = threading.Thread(target=log_watcher, daemon=True)
    watcher.start()
    logger.info("[THREAD] log_watcher 已启动")

    # 信号清理
    def cleanup(signum: int = None, frame: Any = None) -> None:
        logger.info("停止 Web 服务 (PID %s)", os.getpid())
        try:
            os.remove(PID_FILE)
        except FileNotFoundError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    # 启动 HTTP 服务
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    # IPv6+IPv4 双栈 (bind to :: for dual-stack on Linux)
    import socket
    class DualStackServer(http.server.ThreadingHTTPServer):
        address_family = socket.AF_INET6
        allow_reuse_address = True
    server = DualStackServer(('::', PORT), MonitorHandler)
    logger.info("仪表盘启动: http://0.0.0.0:%d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        cleanup()


if __name__ == '__main__':
    main()
