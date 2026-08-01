"""
targets_manager.py — 监控目标配置读写
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

from config import MONITOR_CONF, MONITOR_DEFAULT_TARGETS

logger = logging.getLogger(__name__)


def read_monitor_targets() -> Dict[str, Any]:
    """读取监控目标配置，返回 dict"""
    default = json.loads(json.dumps(MONITOR_DEFAULT_TARGETS))  # 深拷贝默认配置
    if not os.path.exists(MONITOR_CONF):
        return default
    try:
        with open(MONITOR_CONF, 'r', encoding='utf-8') as f:
            saved = json.load(f)
        # 深度合并: saved 覆盖 default 的对应键
        _deep_merge(default, saved)
        return default
    except (OSError, ValueError) as e:
        logger.warning("监控目标配置读取失败, 回退默认: %s", e)
        return default


def write_monitor_targets(data: Dict[str, Any]) -> None:
    """保存监控目标配置"""
    os.makedirs(os.path.dirname(MONITOR_CONF), exist_ok=True)
    with open(MONITOR_CONF, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> None:
    """递归合并 override 到 base（原地修改 base）"""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
