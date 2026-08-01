"""
ftp_manager.py — FTP 配置读写、连接测试、日志上传

优化要点:
- FTP 连接统一用 try/finally 或上下文确保关闭（修复资源泄漏隐患）
- 统一 logging；异常具体化；类型注解
- 上传目录创建逻辑收敛为辅助函数
"""
from __future__ import annotations

import ftplib
import json
import logging
import os
from typing import Dict, List, Tuple

from config import FTP_CONF, FTP_DEFAULT, LOG_DIR

logger = logging.getLogger(__name__)

# 上传失败消息前缀（避免重复拼接字符串字面量）
MSG_CONN_FAIL = "连接失败: "
MSG_UPLOAD_FAIL = "上传失败: "

Fname = str
UploadResult = Tuple[bool, str]


def read_ftp_config() -> Dict[str, any]:
    """读取 FTP 配置，返回 dict（缺字段用默认值补全）"""
    default = dict(FTP_DEFAULT)
    if not os.path.exists(FTP_CONF):
        return default
    try:
        with open(FTP_CONF, 'r', encoding='utf-8') as f:
            saved = json.load(f)
        for k in default:
            if k not in saved:
                saved[k] = default[k]
        return saved
    except (OSError, ValueError) as e:
        logger.warning("FTP 配置读取失败, 回退默认: %s", e)
        return default


def write_ftp_config(cfg: Dict[str, any]) -> None:
    """保存 FTP 配置到文件"""
    os.makedirs(os.path.dirname(FTP_CONF), exist_ok=True)
    with open(FTP_CONF, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def test_ftp_connection(host: str, port: any, user: str, password: str) -> Tuple[bool, str]:
    """测试 FTP 连接，返回 (success, message)"""
    try:
        ftp = ftplib.FTP()
        ftp.connect(host, int(port), timeout=10)
        ftp.login(user, password)
        welcome = ftp.getwelcome()
        ftp.quit()
        return True, "连接成功: " + welcome
    except (OSError, ValueError) as e:
        return False, MSG_CONN_FAIL + str(e)


def _ensure_remote_dir(ftp: ftplib.FTP, remote_path: str) -> None:
    """确保远端目录存在（逐级创建），失败不抛异常，由上层记录"""
    parts = remote_path.strip("/").split("/")
    current = ""
    for p in parts:
        current += "/" + p
        try:
            ftp.cwd(current)
        except ftplib.error_perm:
            try:
                ftp.mkd(current)
                ftp.cwd(current)
            except ftplib.error_perm as e:
                logger.warning("创建远端目录失败 %s: %s", current, e)
                return


def upload_file_to_ftp(local_path: str, cfg: Dict[str, any]) -> UploadResult:
    """上传单个文件到 FTP，返回 (success, message)"""
    if not cfg.get("enabled"):
        return False, "FTP 未启用"
    host = cfg["host"]
    port = int(cfg["port"])
    user = cfg["user"]
    pwd = cfg["password"]
    remote_path = cfg.get("remote_path", "/").rstrip("/")
    fname = os.path.basename(local_path)

    ftp = ftplib.FTP()
    try:
        ftp.connect(host, port, timeout=30)
        ftp.login(user, pwd)
        _ensure_remote_dir(ftp, remote_path)
        with open(local_path, 'rb') as f:
            ftp.storbinary("STOR " + fname, f)
        return True, "上传成功: " + fname + " -> " + remote_path + "/" + fname
    except (OSError, ValueError, ftplib.Error) as e:
        return False, MSG_UPLOAD_FAIL + str(e)
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001 — 关闭连接不应影响已记录的成败
            pass


def upload_logs_to_ftp(cfg: Dict[str, any]) -> List[Dict[str, any]]:
    """
    上传所有日志和趋势数据文件到 FTP，返回结果列表。
    包含: current.log, current.log.N(.gz), monitor_*.log(.gz),
          data/history.jsonl (趋势数据), snapshots/ 下的文件。
    """
    from config import DATA_DIR

    results: List[Dict[str, any]] = []
    if not cfg.get("enabled"):
        return results
    if not os.path.isdir(LOG_DIR):
        return results

    files_to_upload: List[str] = []

    # 1) LOG_DIR 根目录: 日志文件
    for fname in os.listdir(LOG_DIR):
        fpath = os.path.join(LOG_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        if (fname == "current.log"
                or fname.startswith("current.log.")
                or fname.startswith("monitor_")
                or fname.endswith(".log") or fname.endswith(".log.gz")
                or fname.endswith(".gz")):
            files_to_upload.append(fpath)

    # 2) data/ 子目录: 趋势数据
    if os.path.isdir(DATA_DIR):
        for fname in os.listdir(DATA_DIR):
            fpath = os.path.join(DATA_DIR, fname)
            if os.path.isfile(fpath) and (fname.endswith(".jsonl") or fname.endswith(".json")):
                files_to_upload.append(fpath)

    # 3) snapshots/ 子目录: 快照文件 (如果存在)
    snapshot_dir = os.path.join(LOG_DIR, "snapshots")
    if os.path.isdir(snapshot_dir):
        for fname in os.listdir(snapshot_dir):
            fpath = os.path.join(snapshot_dir, fname)
            if os.path.isfile(fpath):
                files_to_upload.append(fpath)

    for fpath in sorted(files_to_upload):
        ok, msg = upload_file_to_ftp(fpath, cfg)
        results.append({"file": os.path.basename(fpath), "ok": ok, "msg": msg})
    return results
