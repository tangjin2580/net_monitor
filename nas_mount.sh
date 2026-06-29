#!/usr/bin/env bash
# nas_mount.sh — NAS 软挂载辅助脚本 (soft 模式, 避免挂载失败导致程序挂起)
# 用法: sudo bash nas_mount.sh [mount|umount|status]

NAS_MOUNT="/mnt/nas_netmon"
NAS_SHARE="//192.168.31.5/fs/1000/nfs"
NAS_MOUNT_OPTS="soft,timeo=10,intr,vers=3,proto=tcp,rsize=8192,wsize=8192"
USE_NAS_FALLBACK=true

set -euo pipefail

action="${1:-status}"

mount_nas() {
    echo "[nas_mount] 尝试挂载 $NAS_SHARE → $NAS_MOUNT"
    mkdir -p "$NAS_MOUNT" 2>/dev/null || true

    # 先检查是否已挂载
    if mountpoint -q "$NAS_MOUNT" 2>/dev/null; then
        echo "[nas_mount] 已挂载: $NAS_MOUNT"
        return 0
    fi

    # 尝试 NFSv4
    if mount -t nfs4 -o "$NAS_MOUNT_OPTS" "$NAS_SHARE" "$NAS_MOUNT" 2>/dev/null; then
        echo "[nas_mount] NFSv4 挂载成功"
        return 0
    fi

    # 回退 NFSv3
    if mount -t nfs -o "$NAS_MOUNT_OPTS" "$NAS_SHARE" "$NAS_MOUNT" 2>/dev/null; then
        echo "[nas_mount] NFSv3 挂载成功"
        return 0
    fi

    # 尝试 CIFS (smb)
    if command -v mount.cifs >/dev/null 2>&1; then
        if mount -t cifs -o "soft,timeo=10,guest,vers=3.0" "$NAS_SHARE" "$NAS_MOUNT" 2>/dev/null; then
            echo "[nas_mount] CIFS 挂载成功"
            return 0
        fi
    fi

    echo "[nas_mount] 所有挂载方式均失败, 将使用本地存储"
    return 1
}

umount_nas() {
    echo "[nas_mount] 卸载 $NAS_MOUNT"
    if mountpoint -q "$NAS_MOUNT" 2>/dev/null; then
        umount "$NAS_MOUNT" 2>/dev/null || umount -l "$NAS_MOUNT" 2>/dev/null || true
        echo "[nas_mount] 卸载完成"
    else
        echo "[nas_mount] 未挂载"
    fi
}

status_nas() {
    echo "[nas_mount] 状态检查:"
    if mountpoint -q "$NAS_MOUNT" 2>/dev/null; then
        echo "  状态: 已挂载"
        df -h "$NAS_MOUNT" 2>/dev/null || true
        mount | grep "$NAS_MOUNT" 2>/dev/null || true
    else
        echo "  状态: 未挂载"
    fi
    echo "  本地回退: $USE_NAS_FALLBACK"
    echo "  本地日志目录: /home/li/net_monitor_logs"
}

case "$action" in
    mount)
        mount_nas
        ;;
    umount)
        umount_nas
        ;;
    status)
        status_nas
        ;;
    *)
        echo "用法: $0 [mount|umount|status]"
        exit 1
        ;;
esac
