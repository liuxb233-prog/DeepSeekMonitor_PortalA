# -*- coding: utf-8 -*-
"""从本地浏览器提取 platform.deepseek.com 的登录态（userToken）。

这是本方案唯一带点"灰色"的地方：DeepSeek 没有公开用量查询 API，
用量数据只存在于开放平台的私有面板接口里，而这些接口只认网页登录态。
所以只能从浏览器自己的 localStorage 里把它读出来——数据不出本机。

顺带说明：值在 localStorage 里常被包成 {"value":X,"__version":"N"} 的形式，
需要剥一层。
"""

import json

from . import chromestore

__all__ = ["ORIGIN", "extract_user_token", "unwrap_appkit", "USER_TOKEN_KEY"]

ORIGIN = "https://platform.deepseek.com"
USER_TOKEN_KEY = "userToken"


def unwrap_appkit(value):
    """剥离 appKit 的 {"value": X, "__version": "N"} 包装；不是则原样返回。"""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return value
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return value
    if isinstance(obj, dict) and "value" in obj and "__version" in obj:
        return obj["value"]
    return value


def extract_user_token():
    """扫描所有浏览器，返回 (token, 描述)。

    找不到时返回 (None, 失败原因)。
    """
    problems = []
    results = chromestore.read_origin_localstorage(ORIGIN)
    if not results:
        return None, (
            "没有在任何浏览器里找到 platform.deepseek.com 的本地存储。"
            "请先用 Chrome 打开 https://platform.deepseek.com 并登录一次。"
        )

    for store, items, errors in results:
        raw = items.get(USER_TOKEN_KEY)
        if not raw:
            problems.append(f"{store.browser}/{store.profile} 里没有 {USER_TOKEN_KEY}")
            continue
        token = unwrap_appkit(raw)
        if not token or not isinstance(token, str) or len(token) < 20:
            problems.append(f"{store.browser}/{store.profile} 里 {USER_TOKEN_KEY} 内容异常")
            continue
        desc = f"{store.browser} / {store.profile}"
        if errors:
            desc += f"（{len(errors)} 个文件解析告警）"
        return token, desc

    detail = "；".join(problems) if problems else "未知原因"
    return None, f"找到了存储但没取到登录态：{detail}"


def extract_profile_info():
    """顺带取出用户 ID 与偏好货币，便于界面展示。"""
    info = {}
    for store, items, _errors in chromestore.read_origin_localstorage(ORIGIN):
        for key, out_key in (
            ("__appKit_userInfo", "userId"),
            ("userCurrencyInLastSession", "currency"),
        ):
            if key in items and out_key not in info:
                v = unwrap_appkit(items[key])
                if out_key == "userId" and isinstance(v, dict):
                    v = v.get("id")
                info[out_key] = v
        if len(info) >= 2:
            break
    return info
