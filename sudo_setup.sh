#!/usr/bin/env bash
# sudo_setup.sh — 需要 root 权限执行的系统配置
# 用法: sudo bash /home/li/sudo_setup.sh
###############################################################################
set -euo pipefail

echo "=========================================="
echo "  net_monitor 系统配置脚本"
echo "=========================================="

# ─── 1. 增加 Swap 空间 ────────────────────────────────────────────────────
echo ""
echo "[1/3] 配置 Swap..."
if ! swapon --show=NAME,SIZE | grep -q swapfile2; then
    fallocate -l 2G /swapfile2
    chmod 600 /swapfile2
    mkswap /swapfile2
    swapon /swapfile2
    if ! grep -q swapfile2 /etc/fstab; then
        echo "/swapfile2 none swap sw 0 0" >> /etc/fstab
    fi
    echo "✅ Swap 已增加: /swapfile2 2GB"
else
    echo "✅ Swapfile2 已存在"
fi
swapon --show

# ─── 2. 安装 systemd 服务 ─────────────────────────────────────────────────
echo ""
echo "[2/3] 安装 systemd 服务..."
cp /home/li/net_monitor.service /etc/systemd/system/net_monitor.service
cp /home/li/net_monitor_web.service /etc/systemd/system/net_monitor_web.service
systemctl daemon-reload
systemctl enable net_monitor.service
systemctl enable net_monitor_web.service

echo "✅ systemd 服务已安装并启用开机自启"
echo ""
echo "   注意: 使用 systemd 后，请用以下命令管理服务:"
echo "   sudo systemctl start net_monitor       # 启动监控"
echo "   sudo systemctl stop net_monitor        # 停止监控"
echo "   sudo systemctl restart net_monitor     # 重启监控"
echo "   sudo systemctl status net_monitor_web  # Web 状态"
echo "   sudo journalctl -u net_monitor -f        # 查看日志"
echo ""
echo "   建议: 停止 net_monitor_ctl.sh 管理的进程，改用 systemd:"
echo "   bash /home/li/net_monitor_ctl.sh stop-all"
echo "   sudo systemctl start net_monitor"
echo "   sudo systemctl start net_monitor_web"

# ─── 3. 配置 logrotate ───────────────────────────────────────────────────
echo ""
echo "[3/3] 配置 logrotate..."
cat > /etc/logrotate.d/net_monitor << 'EOF'
/home/li/net_monitor_logs/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 0644 li li
    sharedscripts
    postrotate
        # 日志轮转后自动触发 FTP 上传 (日志 + 趋势数据)
        curl -s --connect-timeout 10 http://127.0.0.1:9091/api/ftp_upload > /dev/null 2>&1 || true
    endscript
}
EOF
echo "✅ logrotate 已配置: /etc/logrotate.d/net_monitor"

echo ""
echo "=========================================="
echo "  系统配置完成!"
echo "=========================================="
echo ""
echo "请执行以下命令切换到 systemd 管理:"
echo "  bash /home/li/net_monitor_ctl.sh stop-all"
echo "  sudo systemctl start net_monitor"
echo "  sudo systemctl start net_monitor_web"
echo ""
