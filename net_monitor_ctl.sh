#!/usr/bin/env bash
# net_monitor_ctl.sh — 监控管理脚本 (含 Web 仪表盘)
# 用法: bash net_monitor_ctl.sh {start|stop|restart|status|log|tail|events|disconnects|stats|web-start|web-stop|web-restart|web-status}

SCRIPT="/home/li/net_monitor.sh"
WEB_SCRIPT="/home/li/net_monitor_web.py"
PID_FILE="/home/li/net_monitor.pid"
WEB_PID_FILE="/home/li/net_monitor_web.pid"
LOG_DIR="/home/li/net_monitor_logs"
LOG_FILE="${LOG_DIR}/current.log"
WEB_PORT=9091

case "${1:-status}" in
    start)
        if [[ -f "$PID_FILE" ]] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
            echo "监控已在运行 (PID $(cat $PID_FILE))"
            exit 0
        fi
        echo "启动监控..."
        nohup bash "$SCRIPT" > /dev/null 2>&1 &
        sleep 1
        if [[ -f "$PID_FILE" ]]; then
            echo "监控已启动 (PID $(cat $PID_FILE))"
        else
            echo "启动失败，请检查 $SCRIPT"
        fi
        ;;
    stop)
        stopped=false
        if [[ -f "$PID_FILE" ]]; then
            pid=$(cat "$PID_FILE")
            echo "停止监控 (PID $pid)..."
            kill "$pid" 2>/dev/null
            sleep 1
            kill "$pid" 2>/dev/null
            rm -f "$PID_FILE"
            stopped=true
        fi
        # 清理任何残留的僵尸进程（防止 PID 文件丢失导致孤儿进程堆积）
        pkill -f "bash.*net_monitor\.sh" 2>/dev/null && stopped=true
        if $stopped; then
            echo "监控已停止"
        else
            echo "监控未运行"
        fi
        ;;
    restart)
        bash "$0" stop
        sleep 1
        bash "$0" start
        ;;
    status)
        if [[ -f "$PID_FILE" ]] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
            pid=$(cat "$PID_FILE")
            echo "监控运行中 (PID $pid)"
            echo ""
            echo "── 子进程 ──"
            ps --ppid "$pid" -o pid,cmd --no-headers 2>/dev/null || true
            echo ""
            echo "── 日志文件 ──"
            ls -lh "$LOG_DIR"/*.log 2>/dev/null | tail -5
            echo ""
            echo "── 最近5行日志 ──"
            tail -5 "$LOG_FILE" 2>/dev/null
        else
            echo "监控未运行"
            [[ -f "$PID_FILE" ]] && echo "(残留 PID 文件: $(cat $PID_FILE))"
        fi
        ;;
    log)
        # 查看全部日志
        if [[ -f "$LOG_FILE" ]]; then
            cat "$LOG_FILE"
        else
            echo "无日志文件"
        fi
        ;;
    tail)
        # 实时跟踪日志
        tail -f "$LOG_FILE"
        ;;
    events)
        # 只看事件和错误
        if [[ -f "$LOG_FILE" ]]; then
            grep -E '\[(EVENT|ERROR|WARN)\]' "$LOG_FILE"
        else
            echo "无日志文件"
        fi
        ;;
    disconnects)
        # 只看断网事件
        if [[ -f "$LOG_FILE" ]]; then
            grep -E '\[(DISCONNECT|INET_DOWN|RECOVER|LINK_DOWN|IP_LOST|MASS_LEAVE)\]' "$LOG_FILE"
        else
            echo "无日志文件"
        fi
        ;;
    stats)
        # 统计摘要
        if [[ -f "$LOG_FILE" ]]; then
            echo "── 日志统计 ──"
            echo "日志行数: $(wc -l < "$LOG_FILE")"
            echo "日志大小: $(du -h "$LOG_FILE" | cut -f1)"
            echo ""
            echo "断网事件次数: $(grep -c 'DISCONNECT\|INET_DOWN\|LINK_DOWN\|IP_LOST' "$LOG_FILE" 2>/dev/null || echo 0)"
            echo "恢复事件次数: $(grep -c 'RECOVER' "$LOG_FILE" 2>/dev/null || echo 0)"
            echo "路由变化次数: $(grep -c 'ROUTE_CHANGE' "$LOG_FILE" 2>/dev/null || echo 0)"
            echo "ARP 变化次数: $(grep -c 'ARP_LOST\|ARP_NEW\|MASS_LEAVE' "$LOG_FILE" 2>/dev/null || echo 0)"
            echo "DNS 失败次数: $(grep -c 'DNS_FAIL\|DNS_DOWN' "$LOG_FILE" 2>/dev/null || echo 0)"
            echo ""
            echo "── 最近的断网/恢复事件 ──"
            grep -E '\[(DISCONNECT|INET_DOWN|RECOVER|LINK_DOWN)\]' "$LOG_FILE" | tail -20
        else
            echo "无日志文件"
        fi
        ;;
    web-start)
        if [[ -f "$WEB_PID_FILE" ]] && kill -0 "$(cat $WEB_PID_FILE)" 2>/dev/null; then
            echo "Web 已在运行 (PID $(cat $WEB_PID_FILE))"
            exit 0
        fi
        echo "启动 Web 仪表盘..."
        nohup python3 "$WEB_SCRIPT" > /dev/null 2>&1 &
        sleep 2
        if [[ -f "$WEB_PID_FILE" ]]; then
            echo "Web 已启动: http://0.0.0.0:${WEB_PORT} (PID $(cat $WEB_PID_FILE))"
        else
            echo "Web 启动失败"
        fi
        ;;
    web-stop)
        if [[ -f "$WEB_PID_FILE" ]]; then
            pid=$(cat "$WEB_PID_FILE")
            echo "停止 Web (PID $pid)..."
            kill "$pid" 2>/dev/null
            sleep 1
            rm -f "$WEB_PID_FILE"
            echo "Web 已停止"
        else
            echo "Web 未运行"
        fi
        ;;
    web-restart)
        bash "$0" web-stop
        sleep 1
        bash "$0" web-start
        ;;
    web-status)
        if [[ -f "$WEB_PID_FILE" ]] && kill -0 "$(cat $WEB_PID_FILE)" 2>/dev/null; then
            echo "Web 运行中 (PID $(cat $WEB_PID_FILE), 端口 $WEB_PORT)"
        else
            echo "Web 未运行"
        fi
        ;;
    start-all)
        bash "$0" start
        bash "$0" web-start
        ;;
    stop-all)
        bash "$0" web-stop
        bash "$0" stop
        ;;
    webhook-test)
        echo "发送 Webhook 测试消息..."
        curl -s "http://127.0.0.1:${WEB_PORT}/api/webhook-test" || echo "Web 服务未响应"
        ;;
    webhook-status)
        WEBHOOK_CONF="/home/li/net_monitor_logs/webhook.conf"
        if [[ -f "$WEBHOOK_CONF" ]]; then
            echo "── Webhook 配置 ──"
            sed -n '1p' "$WEBHOOK_CONF" | sed 's/^/URL: /'
            sed -n '2p' "$WEBHOOK_CONF" | sed 's/^/平台: /'
            sed -n '3p' "$WEBHOOK_CONF" | sed 's/^/状态: /'
        else
            echo "Webhook 未配置"
            echo "配置路径: $WEBHOOK_CONF"
        fi
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status|log|tail|events|disconnects|stats|web-start|web-stop|web-restart|start-all|stop-all|webhook-test|webhook-status}"
        ;;
esac
