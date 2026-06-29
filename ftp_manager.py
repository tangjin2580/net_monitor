"""
ftp_manager.py — FTP 配置读写、连接测试、日志上传
"""
import json
import os

from config import FTP_CONF, FTP_DEFAULT, LOG_DIR


def read_ftp_config():
    """读取 FTP 配置，返回 dict"""
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
    except Exception:
        return default


def write_ftp_config(cfg):
    """保存 FTP 配置到文件"""
    os.makedirs(os.path.dirname(FTP_CONF), exist_ok=True)
    with open(FTP_CONF, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def test_ftp_connection(host, port, user, password):
    """测试 FTP 连接，返回 (success, message)"""
    try:
        import ftplib
        ftp = ftplib.FTP()
        ftp.connect(host, int(port), timeout=10)
        ftp.login(user, password)
        welcome = ftp.getwelcome()
        try:
            ftp.cwd("/")
        except Exception:
            pass
        ftp.quit()
        return True, "连接成功: " + welcome
    except Exception as e:
        return False, "连接失败: " + str(e)


def upload_file_to_ftp(local_path, cfg):
    """上传单个文件到 FTP，返回 (success, message)"""
    if not cfg.get("enabled"):
        return False, "FTP 未启用"
    try:
        import ftplib
        host = cfg["host"]
        port = int(cfg["port"])
        user = cfg["user"]
        pwd = cfg["password"]
        remote_path = cfg.get("remote_path", "/").rstrip("/")
        fname = os.path.basename(local_path)

        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=30)
        ftp.login(user, pwd)

        try:
            ftp.cwd(remote_path)
        except Exception:
            parts = remote_path.strip("/").split("/")
            current = ""
            for p in parts:
                current += "/" + p
                try:
                    ftp.cwd(current)
                except Exception:
                    try:
                        ftp.mkd(current)
                        ftp.cwd(current)
                    except Exception:
                        pass

        with open(local_path, 'rb') as f:
            ftp.storbinary("STOR " + fname, f)
        ftp.quit()
        return True, "上传成功: " + fname + " -> " + remote_path + "/" + fname
    except Exception as e:
        return False, "上传失败: " + str(e)


def upload_logs_to_ftp(cfg):
    """
    上传所有日志和趋势数据文件到 FTP，返回结果列表。
    包含: current.log, current.log.N(.gz), monitor_*.log(.gz),
          data/history.jsonl (趋势数据), snapshots/ 下的文件。
    """
    from config import DATA_DIR

    results = []
    if not cfg.get("enabled"):
        return results
    if not os.path.isdir(LOG_DIR):
        return results

    files_to_upload = []

    # 1) LOG_DIR 根目录: 日志文件
    for fname in os.listdir(LOG_DIR):
        fpath = os.path.join(LOG_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        # 日志文件: current.log, current.log.N, current.log.N.gz, monitor_*.log(.gz), *.log
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
