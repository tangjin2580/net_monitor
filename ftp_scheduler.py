"""
ftp_scheduler.py — FTP 每日定时归档线程
每天凌晨 00:30 自动上传日志 + 趋势数据到 FTP (logrotate 在 00:00 轮转后)。
用日期标记文件确保重启后不会重复上传。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List

from config import LOG_DIR
from ftp_manager import read_ftp_config, upload_logs_to_ftp

logger = logging.getLogger(__name__)

# ─── 命名常量 ──────────────────────────────────────────────────────────
UPLOAD_HOUR: int = 0
UPLOAD_MINUTE: int = 30
UPLOAD_WINDOW_MINUTES: int = 15      # 触发窗口 (00:30 ~ 00:45)
CHECK_INTERVAL: int = 300           # 每 5 分钟检查一次
MARKER_FILE = os.path.join(LOG_DIR, ".ftp_last_upload")


def _today_str() -> str:
    return time.strftime("%Y-%m-%d")


def _should_upload() -> bool:
    """检查是否到了上传时间且今天还没上传过"""
    now = time.localtime()
    if (now.tm_hour != UPLOAD_HOUR
            or now.tm_min < UPLOAD_MINUTE
            or now.tm_min > UPLOAD_MINUTE + UPLOAD_WINDOW_MINUTES):
        return False
    today = _today_str()
    try:
        with open(MARKER_FILE, "r") as f:
            last = f.read().strip()
        if last == today:
            return False
    except (FileNotFoundError, OSError, ValueError):
        pass
    return True


def _mark_uploaded() -> None:
    """标记今天已上传"""
    try:
        with open(MARKER_FILE, "w") as f:
            f.write(_today_str())
    except OSError as e:
        logger.error("FTP 上传标记写入失败: %s", e)


def ftp_daily_uploader() -> None:
    """后台线程: 每 CHECK_INTERVAL 秒检查一次是否到上传时间"""
    logger.info("每日 FTP 归档线程启动, 计划时间 %02d:%02d", UPLOAD_HOUR, UPLOAD_MINUTE)
    while True:
        try:
            if _should_upload():
                cfg = read_ftp_config()
                if not cfg.get("enabled"):
                    logger.info("FTP 未启用, 跳过")
                    _mark_uploaded()
                else:
                    logger.info("开始每日归档上传...")
                    results: List[Dict[str, Any]] = upload_logs_to_ftp(cfg)
                    ok_count = sum(1 for r in results if r["ok"])
                    fail_count = len(results) - ok_count
                    logger.info("上传完成: %d 成功, %d 失败, 共 %d 文件",
                                ok_count, fail_count, len(results))
                    for r in results:
                        if not r["ok"]:
                            logger.warning("  [FAIL] %s: %s", r["file"], r["msg"])
                    _mark_uploaded()
        except Exception:  # noqa: BLE001 — 调度线程不应因单次异常退出
            logger.exception("FTP 归档异常")

        time.sleep(CHECK_INTERVAL)
