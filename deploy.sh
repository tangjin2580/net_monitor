#!/usr/bin/env bash
###############################################################################
# deploy.sh v3 — 一键部署 net_monitor 到远程服务器 (模块化版本)
# 新增: Python 模块拆分、templates 目录、NAS 软挂载
# 用法:
#   bash deploy.sh                    # 部署到默认目标 li@192.168.31.251
#   bash deploy.sh user@host          # 部署到指定目标
#   bash deploy.sh user@host --key ~/.ssh/id_ed25519  # 指定 SSH 密钥
###############################################################################
set -euo pipefail

# ─── 默认配置 ─────────────────────────────────────────────────────────
DEFAULT_TARGET="li@192.168.31.251"
DEFAULT_SSH_KEY="$HOME/.ssh/key/id_ed25519"
REMOTE_DIR="/home/li"
WEB_PORT=9091

# ─── 参数解析 ─────────────────────────────────────────────────────────
TARGET="${1:-$DEFAULT_TARGET}"
SSH_KEY="$DEFAULT_SSH_KEY"
if [[ "${2:-}" == "--key" ]]; then
    SSH_KEY="${3:-$DEFAULT_SSH_KEY}"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

SSH_OPTS="-i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10"
SCP_OPTS="-i $SSH_KEY -o StrictHostKeyChecking=no"

echo "============================================"
echo "  net_monitor 部署脚本 v3 (模块化)"
echo "============================================"
echo "目标: $TARGET"
echo "SSH 密钥: $SSH_KEY"
echo "远程目录: $REMOTE_DIR"
echo ""

# ─── 检查文件 ─────────────────────────────────────────────────────────
CORE_FILES="net_monitor.sh net_monitor_web.py net_monitor_ctl.sh nas_mount.sh"
PY_MODULES="config.py log_parser.py system_stats.py ikuai_client.py ftp_manager.py traceroute_parser.py web_handler.py"
WEB_FILES="dashboard.html ikuai.html ftp_config.html"

for f in $CORE_FILES $PY_MODULES; do
    if [[ ! -f "$SCRIPT_DIR/$f" ]]; then
        echo "[ERROR] 缺少文件: $SCRIPT_DIR/$f"
        exit 1
    fi
done
if [[ ! -d "$SCRIPT_DIR/templates" ]]; then
    echo "[ERROR] 缺少目录: $SCRIPT_DIR/templates"
    exit 1
fi
echo "[OK] 所有文件就绪"

# ─── SSH 连通性测试 ─────────────────────────────────────────────────
echo -n "[...] 测试 SSH 连接... "
if ! ssh $SSH_OPTS "$TARGET" "echo ok" > /dev/null 2>&1; then
    echo "FAIL"
    echo "[ERROR] SSH 连接失败: $TARGET (密钥: $SSH_KEY)"
    exit 1
fi
echo "OK"

# ─── 停止现有服务 ─────────────────────────────────────────────────────
echo "[...] 停止现有服务..."
ssh $SSH_OPTS "$TARGET" bash -s << 'REMOTE_STOP'
    # 停止所有 net_monitor 进程 (防止多实例)
    pkill -f 'net_monitor.sh' 2>/dev/null || true
    pkill -f 'net_monitor_web.py' 2>/dev/null || true
    sleep 2
    # 确认清理
    remaining=$(ps aux | grep net_monitor | grep -v grep | wc -l)
    if (( remaining > 0 )); then
        echo "[WARN] 仍有 $remaining 个残留进程, 强制清理..."
        pkill -9 -f net_monitor 2>/dev/null || true
        sleep 1
    fi
    echo "[OK] 所有旧进程已清理"
REMOTE_STOP

# ─── 上传文件 ─────────────────────────────────────────────────────
echo "[...] 上传文件..."
# 核心脚本
for f in $CORE_FILES; do
    scp $SCP_OPTS "$SCRIPT_DIR/$f" "$TARGET:$REMOTE_DIR/$f"
done
# Python 模块
for f in $PY_MODULES; do
    scp $SCP_OPTS "$SCRIPT_DIR/$f" "$TARGET:$REMOTE_DIR/$f"
done
# Web 页面 (部署到 /opt/net_monitor/)
ssh $SSH_OPTS "$TARGET" "mkdir -p /opt/net_monitor/templates"
for f in $WEB_FILES; do
    if [[ -f "$SCRIPT_DIR/$f" ]]; then
        scp $SCP_OPTS "$SCRIPT_DIR/$f" "$TARGET:/opt/net_monitor/$f"
    fi
done
# 内嵌模板
for f in "$SCRIPT_DIR"/templates/*.html; do
    if [[ -f "$f" ]]; then
        scp $SCP_OPTS "$f" "$TARGET:/opt/net_monitor/templates/"
    fi
done
# 同时部署到 $REMOTE_DIR (web_handler 的 SCRIPT_DIR 需要找到 templates/)
ssh $SSH_OPTS "$TARGET" "mkdir -p $REMOTE_DIR/templates"
for f in "$SCRIPT_DIR"/templates/*.html; do
    if [[ -f "$f" ]]; then
        scp $SCP_OPTS "$f" "$TARGET:$REMOTE_DIR/templates/"
    fi
done
echo "[OK] 文件上传完成"

# ─── 设置权限 ─────────────────────────────────────────────────────
echo "[...] 设置权限..."
ssh $SSH_OPTS "$TARGET" "chmod +x $REMOTE_DIR/net_monitor.sh $REMOTE_DIR/net_monitor_ctl.sh $REMOTE_DIR/nas_mount.sh"
echo "[OK] 权限设置完成"

# ─── NAS 软挂载 (soft 模式) ───────────────────────────────────
echo "[...] 设置 NAS 软挂载 (soft 模式)..."
ssh $SSH_OPTS "$TARGET" bash -s << 'REMOTE_NAS'
    # 创建挂载点
    sudo mkdir -p /mnt/nas_netmon 2>/dev/null || true

    # 尝试 soft 挂载 (不会阻塞)
    if sudo bash "$HOME/nas_mount.sh" mount 2>/dev/null; then
        echo "[OK] NAS 挂载成功 (soft 模式)"
    else
        echo "[WARN] NAS 挂载失败, 将使用本地日志存储 (soft 回退)"
    fi

    # 设置自动挂载 (systemd automount, soft)
    if command -v systemctl >/dev/null 2>&1; then
        echo "[...] 配置 systemd automount (soft)..."
        sudo tee /etc/systemd/system/mnt-nas_netmon.mount > /dev/null << MOUNT_UNIT
[Unit]
Description=NAS netmon (soft mount)
After=network-online.target
Wants=network-online.target

[Mount]
What=//192.168.31.5/fs/1000/nfs
Where=/mnt/nas_netmon
Type=nfs4
Options=soft,timeo=10,intr,vers=3,proto=tcp

[Install]
WantedBy=multi-user.target
MOUNT_UNIT

        sudo tee /etc/systemd/system/mnt-nas_netmon.automount > /dev/null << AUTO_UNIT
[Unit]
Description=Auto-mount NAS netmon (soft)
After=network-online.target

[Automount]
Where=/mnt/nas_netmon
TimeoutIdleSec=300

[Install]
WantedBy=multi-user.target
AUTO_UNIT

        sudo systemctl daemon-reload 2>/dev/null || true
        sudo systemctl enable mnt-nas_netmon.automount 2>/dev/null || true
        sudo systemctl start mnt-nas_netmon.automount 2>/dev/null || true
        echo "[OK] systemd automount 已配置 (soft 模式)"
    else
        echo "[INFO] systemd 不可用, 跳过 automount 配置"
        echo "[INFO] 请在 /etc/fstab 中添加:"
        echo "  //192.168.31.5/fs/1000/nfs /mnt/nas_netmon nfs4 soft,timeo=10,intr,vers=3,_netdev 0 0"
    fi
REMOTE_NAS

# ─── 创建目录 ─────────────────────────────────────────────────────
echo "[...] 创建数据目录..."
ssh $SSH_OPTS "$TARGET" "mkdir -p $REMOTE_DIR/net_monitor_logs/trace_baselines $REMOTE_DIR/net_monitor_logs/trace_current $REMOTE_DIR/net_monitor_logs/snapshots"
echo "[OK] 目录就绪"

# ─── 清理旧日志 (可选, 保留最近 3 天) ─────────────────────
echo "[...] 清理旧日志 (>3天)..."
ssh $SSH_OPTS "$TARGET" "find $REMOTE_DIR/net_monitor_logs -name 'monitor_*.log' -mtime +3 -delete 2>/dev/null || true"
echo "[OK] 日志清理完成"

# ─── 启动服务 ─────────────────────────────────────────────────────
echo "[...] 启动监控服务..."
ssh $SSH_OPTS "$TARGET" "bash $REMOTE_DIR/net_monitor_ctl.sh start"
sleep 2

echo "[...] 启动 Web 仪表盘..."
ssh $SSH_OPTS "$TARGET" "bash $REMOTE_DIR/net_monitor_ctl.sh web-start"
sleep 1

# ─── 验证 ─────────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  部署验证"
echo "============================================"
ssh $SSH_OPTS "$TARGET" bash -s << REMOTE_VERIFY
    echo "── 进程状态 ──"
    ps aux | grep -E 'net_monitor\.(sh|py)' | grep -v grep | head -10
    echo ""
    echo "── NAS 挂载状态 ──"
    mountpoint -q /mnt/nas_netmon 2>/dev/null && echo "  NAS: 已挂载 ✓" || echo "  NAS: 未挂载 (将使用本地存储) ∼"
    echo ""
    echo "── 最新日志 ──"
    tail -5 $REMOTE_DIR/net_monitor_logs/current.log 2>/dev/null || echo "(暂无日志)"
    echo ""
    echo "── Web 端口 ──"
    ss -tlnp | grep ":$WEB_PORT " || echo "(端口未监听)"
REMOTE_VERIFY

echo ""
echo "============================================"
echo "  部署完成! (v3 - 模块化 + NAS soft 挂载)"
echo "============================================"
echo ""
echo "  Web 仪表盘: http://${TARGET#*@}:${WEB_PORT}"
echo "  日志查看:   ssh ${TARGET} 'tail -f ${REMOTE_DIR}/net_monitor_logs/current.log'"
echo "  控制命令:   ssh ${TARGET} 'bash ${REMOTE_DIR}/net_monitor_ctl.sh status'"
echo "  NAS 挂载:  ssh ${TARGET} 'bash ${REMOTE_DIR}/nas_mount.sh status'"
echo ""
