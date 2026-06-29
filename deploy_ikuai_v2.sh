#!/usr/bin/env bash
# =============================================================
#  deploy_ikuai_v2.1.sh — 爱快监控 v2.1 部署脚本 (修复LAN口误判)
#  用法: bash deploy_ikuai_v2.sh
#  需要: SSH 密码（脚本会提示输入）
# =============================================================
set -e

SERVER="li@192.168.31.5"
REMOTE_DIR="/home/li"
OPT_DIR="/opt/net_monitor"

echo "=============================================="
echo "  爱快监控 v2 部署脚本"
echo "  服务器: $SERVER"
echo "=============================================="
echo ""

# 检查本地文件
echo "[1/5] 检查本地文件..."
for f in ikuai_mon.py ikuai.html net_monitor.sh net_monitor_web.py; do
    if [[ ! -f "$f" ]]; then
        echo "  ❌ 缺失: $f"
        exit 1
    fi
    echo "  ✅ $f ($(wc -l < "$f") 行)"
done

# 上传文件（需要密码）
echo ""
echo "[2/5] 上传文件到服务器..."
echo "  提示: 请输入 SSH 密码（第一次会提示，后续 scp 可能也需要）"

# 使用 scp 上传（如果需要密码，用户需手动输入）
echo "  → 上传 ikuai_mon.py ..."
scp "ikuai_mon.py" "${SERVER}:${REMOTE_DIR}/ikuai_mon.py" || {
    echo "  ❌ scp 失败，请检查 SSH 连通性"
    exit 1
}

echo "  → 上传 ikuai.html ..."
scp "ikuai.html" "${SERVER}:${OPT_DIR}/ikuai.html" || \
scp "ikuai.html" "${SERVER}:${REMOTE_DIR}/ikuai.html" || echo "  ⚠️  ikuai.html 上传失败（不影响核心功能）"

echo "  → 上传 net_monitor.sh ..."
scp "net_monitor.sh" "${SERVER}:${REMOTE_DIR}/net_monitor.sh" || echo "  ⚠️  net_monitor.sh 上传失败"

echo "  → 上传 net_monitor_web.py ..."
scp "net_monitor_web.py" "${SERVER}:${OPT_DIR}/net_monitor_web.py" || \
scp "net_monitor_web.py" "${SERVER}:${REMOTE_DIR}/net_monitor_web.py" || echo "  ⚠️  net_monitor_web.py 上传失败"

# 在服务器上执行部署
echo ""
echo "[3/5] 在服务器上执行部署..."

ssh "$SERVER" "bash -s" << 'REMOTE_EOF'
    set -e
    echo "  → 停止监控服务..."
    kill $(cat /home/li/net_monitor.pid 2>/dev/null) 2>/dev/null || true
    sleep 2

    echo "  → 清除 Python 字节码缓存..."
    find /opt/net_monitor/ -name "*.pyc" -delete 2>/dev/null || true
    find /home/li/ -name "*.pyc" -delete 2>/dev/null || true

    echo "  → 设置权限..."
    chmod +x /home/li/net_monitor.sh
    chmod +x /home/li/ikuai_mon.py

    echo "  → 启动监控服务..."
    cd /home/li
    nohup bash /home/li/net_monitor.sh > /dev/null 2>&1 &
    sleep 3

    echo "  → 检查服务状态..."
    if pgrep -f "net_monitor.sh" >/dev/null; then
        echo "  ✅ net_monitor.sh 已启动"
    else
        echo "  ❌ net_monitor.sh 启动失败"
        exit 1
    fi

    if pgrep -f "net_monitor_web.py" >/dev/null; then
        echo "  ✅ net_monitor_web.py 已启动"
    else
        echo "  ⚠️  net_monitor_web.py 未运行（可能由 systemd 管理）"
    fi

    if pgrep -f "ikuai_mon.py" >/dev/null; then
        echo "  ✅ ikuai_mon.py 监控进程已启动"
    else
        echo "  ⚠️  ikuai_mon.py 监控进程未运行"
    fi

    echo "  → 检查 联通2 告警监控..."
    if pgrep -f "ikuai_wan_monitor" >/dev/null; then
        echo "  ✅ 联通2 告警监控已启动"
    else
        echo "  ⚠️  联通2 告警监控未运行（检查 net_monitor.sh）"
    fi

    echo ""
    echo "  → 最新日志 (最后 5 行):"
    tail -5 /home/li/net_monitor_logs/current.log 2>/dev/null || echo "    日志文件不存在"
REMOTE_EOF

if [[ $? -eq 0 ]]; then
    echo ""
    echo "[4/5] ✅ 部署成功!"
    echo ""
    echo "→ 检查服务:"
    echo "    SSH 登录后运行: ps aux | grep -E 'net_monitor|ikuai_mon'"
    echo ""
    echo "→ 查看日志:"
    echo "    tail -f /home/li/net_monitor_logs/current.log"
    echo ""
    echo "→ 访问仪表盘:"
    echo "    爱快监控: http://192.168.31.5:7890/ikuai.html"
    echo "    主仪表盘: http://192.168.31.5:7890/"
    echo ""
    echo "→ 联通2 告警:"
    echo "    已集成到 net_monitor.sh，会自动发送 webhook 告警"
    echo "    检查: grep 'WAN2' /home/li/net_monitor_logs/current.log"
else
    echo ""
    echo "[4/5] ❌ 部署失败，请检查 above 错误信息"
    exit 1
fi

echo ""
echo "[5/5] 部署完成!"
echo "=============================================="
