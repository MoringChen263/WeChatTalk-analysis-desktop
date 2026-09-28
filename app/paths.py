# -*- coding: utf-8 -*-
"""路径解析：打包与源码两种运行方式都要能找到 skills/ 与配置目录。

统一规则（零绝对个人路径）：
- 源码运行：项目根 = 本文件上两级
- PyInstaller one-dir：项目根 = sys._MEIPASS（_internal）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "jev-chat-analyzer"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """应用根目录：源码下是仓库根，打包后是 _internal。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def skills_dir() -> Path:
    return app_root() / "skills"


def skill_dir(name: str = "goutoujunshi") -> Path:
    return skills_dir() / name


def user_data_dir() -> Path:
    """用户级数据目录。

    默认 `%APPDATA%/jev-chat-analyzer`；可用环境变量 `JEV_DATA_DIR` 覆盖
    （便携模式：配置/日志跟着程序走；也给自动化测试一个可写位置）。
    """
    override = os.environ.get("JEV_DATA_DIR")
    if override:
        d = Path(override)
    else:
        base = os.environ.get("APPDATA") or str(Path.home())
        d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return user_data_dir() / "config.json"


def log_path() -> Path:
    logs = user_data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "app.log"
