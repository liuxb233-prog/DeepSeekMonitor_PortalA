# -*- coding: utf-8 -*-
"""验证 api.py 的三条选路逻辑，用假响应，不碰网络。

  1. 有平台登录态            -> 走平台，balance_only=False
  2. 平台失效 + 有 API Key   -> 降级到官方余额，balance_only=True，带 warnings
  3. 两者都没有              -> 抛 AuthExpired
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dsmon.api import (DeepSeekClient, AuthExpired,  # noqa: E402
                       to_decimal, _unwrap, ApiError)

FAKE_OFFICIAL = {
    "is_available": True,
    "balance_infos": [
        {"currency": "CNY", "total_balance": "110.0000000000",
         "granted_balance": "10.0000000000", "topped_up_balance": "100.0000000000"},
        {"currency": "USD", "total_balance": "0.00",
         "granted_balance": "0.00", "topped_up_balance": "0.00"},
    ],
}

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(f"  {'ok  ' if cond else 'FAIL'} {name}  {extra}")


class OfficialOnly(DeepSeekClient):
    """模拟：没有平台登录态，只有 API Key。"""
    def fetch_official_balance(self):
        return FAKE_OFFICIAL


class PlatformDead(DeepSeekClient):
    """模拟：平台登录态过期，但有 API Key。"""
    def _snapshot_platform(self, now=None):
        raise AuthExpired("登录态被拒绝（HTTP 401）")

    def fetch_official_balance(self):
        return FAKE_OFFICIAL


class Nothing(DeepSeekClient):
    """模拟：什么都没有。"""
    pass


print("[1] 只有 API Key（无平台登录态）")
c = OfficialOnly(user_token=None, api_key="sk-fake")
snap = c.snapshot()
check("balance_only 为 True", snap.balance_only)
check("拿到 2 个钱包", len(snap.wallets) == 2, f"{[w.currency for w in snap.wallets]}")
check("CNY 余额 = 110", snap.wallet("CNY").balance == to_decimal("110.0"),
      str(snap.wallet("CNY").balance))
check("CNY 赠金 = 10", snap.wallet("CNY").bonus == to_decimal("10.0"))
check("主钱包是 CNY", snap.primary_wallet.currency == "CNY")
check("用量为空", snap.today.total_tokens == 0 and snap.month.total_tokens == 0)
check("页面有耗时记录", snap.elapsed_ms >= 0, f"{snap.elapsed_ms}ms")

print("\n[2] 平台登录态失效 + 有 API Key -> 应降级而非报错")
c2 = PlatformDead(user_token="expired-token", api_key="sk-fake")
snap2 = c2.snapshot()
check("降级成功", snap2.balance_only)
check("自动挑了主钱包", snap2.primary_wallet is not None)
check("warnings 带上了失败原因", len(snap2.warnings) == 1, str(snap2.warnings))

print("\n[3] 两者都没有 -> 应抛 AuthExpired")
c3 = Nothing(user_token=None, api_key=None)
try:
    c3.snapshot()
    check("抛出 AuthExpired", False, "却没抛")
except AuthExpired as e:
    check("抛出 AuthExpired", True, f"「{e}」")

print("\n[4] 平台失效且没有 API Key -> 应抛 AuthExpired")
c4 = PlatformDead(user_token="expired", api_key=None)
try:
    c4.snapshot()
    check("抛出 AuthExpired", False, "却没抛")
except AuthExpired as e:
    check("抛出 AuthExpired", True, f"「{e}」")

print("\n[5] 响应壳的容错")
try:
    _unwrap({"code": 0, "data": {"biz_code": 1, "biz_msg": "INVALID_PARAM",
                                 "biz_data": None}})
    check("biz_code!=0 会报错", False, "却没报")
except ApiError as e:
    check("biz_code!=0 会报错", True, f"「{e}」")

try:
    _unwrap({"code": 0, "data": {"biz_code": 1, "biz_msg": "AUTH_EXPIRED",
                                 "biz_data": None}})
    check("AUTH_EXPIRED 映射为 AuthExpired", False)
except AuthExpired as e:
    check("AUTH_EXPIRED 映射为 AuthExpired", True, f"「{e}」")

print()
failed = [n for n, ok in results if not ok]
print(f"共 {len(results)} 项，失败 {len(failed)} 项" +
      (f"：{failed}" if failed else " —— 全部通过"))
sys.exit(1 if failed else 0)
