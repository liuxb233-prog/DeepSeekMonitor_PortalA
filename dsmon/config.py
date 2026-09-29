# -*- coding: utf-8 -*-
"""配置读写。放在用户目录，不污染项目。"""

import os
import json

__all__ = ["Config", "config_path"]

_DIR = os.path.join(os.path.expanduser("~"), ".deepseek-monitor")
_FILE = os.path.join(_DIR, "config.json")

DEFAULTS = {
    "refresh_seconds": 60,          # 自动刷新间隔
    "window_x": None,               # 窗口位置，None 表示首次自动靠右下角
    "window_y": None,
    "topmost": True,                # 是否置顶
    "alpha": 0.96,                  # 窗口透明度
    "low_balance_threshold": 20.0,  # 低于此余额变红提醒
    "currency": None,               # 主显示币种，None 表示自动挑
    "collapsed": False,             # 是否以收起状态启动
    "api_key": None,                # 可选的官方 API Key（仅用于交叉核对余额）
    "manual_token": None,           # 手动粘贴的平台登录态（自动提取失败时的兜底）
}


def config_path():
    return _FILE


class Config(dict):
    """行为像 dict 的配置对象，带默认值合并与落盘。"""

    def __init__(self, data=None):
        super().__init__(DEFAULTS)
        if data:
            self.update({k: v for k, v in data.items() if k in DEFAULTS})

    @classmethod
    def load(cls):
        try:
            with open(_FILE, encoding="utf-8") as f:
                return cls(json.load(f))
        except (OSError, json.JSONDecodeError):
            return cls()

    def save(self):
        try:
            os.makedirs(_DIR, exist_ok=True)
            tmp = _FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _FILE)
        except OSError:
            pass

    # ---- 便捷访问 ----

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in DEFAULTS:
            self[name] = value
        else:
            super().__setattr__(name, value)
