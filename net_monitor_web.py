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
import os
import signal
import sys
import threading
from datetime import datetime

# ─── 从拆分模块导入 ──────────────────────────────────────────────────
from config import (
    PORT, PID_FILE, LOG_FILE,
    log_lines, parsed_events, stats,
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


# ─── 主入口 ──────────────────────────────────────────────────────────
def main():
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    stats["web_start_time"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 加载历史日志
    if os.path.exists(LOG_FILE):
        print(f"[web] 加载历史日志: {LOG_FILE}")
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                process_line(line)
        print(f"[web] 已加载 {len(log_lines)} 行日志, {len(parsed_events)} 个事件")

    # 启动系统资源采集线程
    t_sys = threading.Thread(target=system_stats_collector, daemon=True, name="sys_stats")
    t_sys.start()
    print("[THREAD] system_stats_collector 已启动")

    # 启动历史趋势记录线程 (CPU/内存/延迟, 7天)
    t_hist = threading.Thread(target=history_recorder, daemon=True, name="history_recorder")
    t_hist.start()
    print("[THREAD] history_recorder 已启动")

    # 启动 FTP 每日定时上传线程 (日志归档 + 趋势数据)
    t_ftp = threading.Thread(target=ftp_daily_uploader, daemon=True, name="ftp_uploader")
    t_ftp.start()
    print("[THREAD] ftp_daily_uploader 已启动")

    # 启动日志监控线程
    watcher = threading.Thread(target=log_watcher, daemon=True)
    watcher.start()
    print("[THREAD] log_watcher 已启动")

    # 信号清理
    def cleanup(signum=None, frame=None):
        print(f"[web] 停止 Web 服务 (PID {os.getpid()})")
        try:
            os.remove(PID_FILE)
        except FileNotFoundError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    # 启动 HTTP 服务
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    server = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), MonitorHandler)
    print(f"[web] 仪表盘启动: http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        cleanup()


if __name__ == '__main__':
    main()
