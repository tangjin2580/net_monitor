"""
ftp_scheduler.py — FTP 每日定时归档线程
每天凌晨 00:30 自动上传日志 + 趋势数据到 FTP (logrotate 在 00:00 轮转后)。
用日期标记文件确保重启后不会重复上传。
"""
import os
import time

from config import LOG_DIR
from ftp_manager import read_ftp_config, upload_logs_to_ftp

# 上传时间 (小时, 24h制), logrotate 00:00 轮转后半小时执行
UPLOAD_HOUR = 0
UPLOAD_MINUTE = 30
# 标记文件: 记录上次上传日期, 防止重启后重复
MARKER_FILE = os.path.join(LOG_DIR, ".ftp_last_upload")


def _today_str():
    return time.strftime("%Y-%m-%d")


def _should_upload():
    """检查是否到了上传时间且今天还没上传过"""
    now = time.localtime()
    # 只在目标时间窗口内触发 (00:30 ~ 00:45)
    if now.tm_hour != UPLOAD_HOUR or now.tm_min < UPLOAD_MINUTE or now.tm_min > UPLOAD_MINUTE + 15:
        return False
    # 检查今天是否已上传
    today = _today_str()
    try:
        with open(MARKER_FILE, "r") as f:
            last = f.read().strip()
        if last == today:
            return False
    except (FileNotFoundError, ValueError):
        pass
    return True


def _mark_uploaded():
    """标记今天已上传"""
    try:
        with open(MARKER_FILE, "w") as f:
            f.write(_today_str())
    except Exception as e:
        print("[ftp_scheduler] 标记写入失败: %s" % e)


def ftp_daily_uploader():
    """后台线程: 每 5 分钟检查一次是否到上传时间"""
    print("[ftp_scheduler] 每日 FTP 归档线程启动, 计划时间 %02d:%02d"
          % (UPLOAD_HOUR, UPLOAD_MINUTE))
    while True:
        try:
            if _should_upload():
                cfg = read_ftp_config()
                if not cfg.get("enabled"):
                    print("[ftp_scheduler] FTP 未启用, 跳过")
                    _mark_uploaded()
                else:
                    print("[ftp_scheduler] 开始每日归档上传...")
                    results = upload_logs_to_ftp(cfg)
                    ok_count = sum(1 for r in results if r["ok"])
                    fail_count = len(results) - ok_count
                    print("[ftp_scheduler] 上传完成: %d 成功, %d 失败, 共 %d 文件"
                          % (ok_count, fail_count, len(results)))
                    if fail_count:
                        for r in results:
                            if not r["ok"]:
                                print("  [FAIL] %s: %s" % (r["file"], r["msg"]))
                    _mark_uploaded()
        except Exception as e:
            print("[ftp_scheduler] 异常: %s" % e)

        # 每 5 分钟检查一次
        time.sleep(300)
