# -*- coding: utf-8 -*-
"""从 Chromium 系浏览器的 Local Storage 里读取指定站点的 localStorage。

Chrome 的 localStorage 存在 LevelDB 里，键的编码规则：
    META:<origin>                     -> 该 origin 的元数据（大小、最后修改时间）
    _<origin>\\x00\\x01<key>           -> 真正的键值对

值的编码：首字节是编码标记，其后是字符串数据（UTF-16LE 或 Latin-1）。
这里不硬编码猜测，而是按标记解，并在解出的结果明显异常时回退。
"""

import os
import json
import glob

from .leveldb import LevelDB, LevelDBError

__all__ = ["find_stores", "read_origin_localstorage", "BrowserStore"]

# Chromium 系浏览器的 User Data 根目录候选
_BROWSER_ROOTS = [
    ("Chrome", r"Google\Chrome\User Data"),
    ("Chrome Beta", r"Google\Chrome Beta\User Data"),
    ("Chrome Dev", r"Google\Chrome Dev\User Data"),
    ("Edge", r"Microsoft\Edge\User Data"),
    ("Brave", r"BraveSoftware\Brave-Browser\User Data"),
    ("Vivaldi", r"Vivaldi\User Data"),
    ("Chromium", r"Chromium\User Data"),
    ("360 极速", r"360Chrome\Chrome\User Data"),
    ("360 安全", r"360ChromeX\Chrome\User Data"),
    ("QQ 浏览器", r"Tencent\QQBrowser\User Data"),
    ("搜狗", r"SogouExplorer\User Data"),
]

_SCAN_BASES = [
    os.environ.get("LOCALAPPDATA", r"C:\Users\%USERNAME%\AppData\Local"),
    os.environ.get("APPDATA", r"C:\Users\%USERNAME%\AppData\Roaming"),
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    os.environ.get("ProgramFiles", r"C:\Program Files"),
]


class BrowserStore:
    """定位到的一个 Local Storage 数据库。"""

    def __init__(self, browser, profile, directory):
        self.browser = browser
        self.profile = profile
        self.directory = directory

    def __repr__(self):
        return f"<{self.browser}/{self.profile} {self.directory}>"

    def load(self, origin):
        """读出该 origin 下的全部 localStorage，返回 (dict, errors)。"""
        db = LevelDB(self.directory)
        prefix = ("_" + origin + "\x00\x01").encode("utf-8")
        items = {}
        for raw_key, raw_value in db.prefix_items(prefix):
            key = raw_key[len(prefix):].decode("utf-8", "replace")
            items[key] = decode_ls_value(raw_value)
        return items, db.errors


def find_stores():
    """扫描所有已知浏览器与 profile，返回存在的 Local Storage 数据库列表。"""
    found = []
    seen = set()
    for base in _SCAN_BASES:
        if not base or not os.path.isdir(base):
            continue
        for browser, rel in _BROWSER_ROOTS:
            udir = os.path.join(base, rel)
            if not os.path.isdir(udir):
                continue
            try:
                entries = os.listdir(udir)
            except OSError:
                continue
            for name in entries:
                if name != "Default" and not name.startswith("Profile "):
                    continue
                lsdir = os.path.join(udir, name, "Local Storage", "leveldb")
                if os.path.isdir(lsdir) and lsdir not in seen:
                    seen.add(lsdir)
                    found.append(BrowserStore(browser, name, lsdir))
    return found


def decode_ls_value(raw):
    """解码 localStorage 的值。

    Chromium 的编码：首字节 0x00 = UTF-16LE，0x01 = Latin-1。
    若解码结果异常（含大量替换字符或不可打印字符），回退为直接 UTF-8 解码。
    """
    if not raw:
        return ""
    tag, body = raw[0], raw[1:]
    try:
        if tag == 0x00:
            text = body.decode("utf-16-le")
        elif tag == 0x01:
            text = body.decode("latin-1")
        else:
            text = raw.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        text = raw.decode("utf-8", "replace")

    # 合理性检查：如果出现大量替换字符，说明猜错了，回退
    if text.count("\ufffd") > len(text) * 0.1 if text else False:
        text = raw.decode("utf-8", "replace")
    return text


def read_origin_localstorage(origin):
    """在全部浏览器/全部 profile 里找某个 origin 的 localStorage。

    返回 [(BrowserStore, {key: value}, errors), ...]，只列出真正读到数据的。
    """
    results = []
    for store in find_stores():
        try:
            items, errors = store.load(origin)
        except (LevelDBError, OSError):
            continue
        if items:
            results.append((store, items, errors))
    return results


# 候选的登录态键名，按优先级排列
TOKEN_KEY_HINTS = [
    "userToken",
    "usertoken",
    "token",
    "access_token",
    "accessToken",
    "__user_token",
]


def looks_like_jwt(value):
    """粗略判断是否像 JWT / 平台会话令牌。"""
    if not value or len(value) < 40:
        return False
    parts = value.split(".")
    if len(parts) == 3 and all(len(p) > 4 for p in parts):
        return True
    # 平台令牌未必是 JWT，长 base64 串也接受
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_\-=+/]{40,}", value))
