# -*- coding: utf-8 -*-
"""DeepSeek 用量与余额的数据层。

两个数据源，权限完全不同：

1. **官方公开接口**（只要 API Key）
       GET https://api.deepseek.com/user/balance
   稳定、有文档，但**只有余额**，拿不到用量。

2. **开放平台私有面板接口**（要网页登录态 userToken）
       GET https://platform.deepseek.com/api/v0/users/get_user_summary
       GET https://platform.deepseek.com/api/v0/usage/amount?month=&year=
       GET https://platform.deepseek.com/api/v0/usage/cost?month=&year=
   余额、token 用量、费用都有，还带 30 天日粒度。代价是这些是私有接口，
   DeepSeek 改版就可能失效——所以解析层写得尽量宽容，失败时明确报错。

金额一律用 Decimal，避免 JSON 里 "0E-16" 这类科学计数法被浮点吃掉精度。
"""

import gzip
import json
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor

__all__ = [
    "DeepSeekClient", "Snapshot", "Usage", "Wallet", "DayUsage",
    "ApiError", "AuthExpired", "NetworkError",
]

PLATFORM_BASE = "https://platform.deepseek.com"
OFFICIAL_BALANCE_URL = "https://api.deepseek.com/user/balance"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 计费条目类型 -> 语义
TYPE_PROMPT = "PROMPT_TOKEN"
TYPE_HIT = "PROMPT_CACHE_HIT_TOKEN"
TYPE_MISS = "PROMPT_CACHE_MISS_TOKEN"
TYPE_RESPONSE = "RESPONSE_TOKEN"
TYPE_REQUEST = "REQUEST"


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------

class ApiError(Exception):
    """接口返回了业务错误。"""


class AuthExpired(ApiError):
    """登录态失效，需要回到浏览器重新登录平台。"""


class NetworkError(ApiError):
    """网络层失败。"""


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------

def to_decimal(text):
    """把接口返回的字符串金额安全转成 Decimal。"""
    if text is None:
        return Decimal(0)
    if isinstance(text, (int, float, Decimal)):
        return Decimal(str(text))
    s = str(text).strip()
    if not s:
        return Decimal(0)
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal(0)


def to_int(text):
    """把 token 数量的字符串安全转成 int（可能带小数如 "1.0E+3"）。"""
    if text is None:
        return 0
    try:
        return int(to_decimal(text))
    except (ValueError, OverflowError):
        return 0


class Usage:
    """一段区间内的 token / 请求用量。"""

    __slots__ = ("requests", "prompt", "cache_hit", "cache_miss", "response")

    def __init__(self, requests=0, prompt=0, cache_hit=0, cache_miss=0, response=0):
        self.requests = requests
        self.prompt = prompt
        self.cache_hit = cache_hit
        self.cache_miss = cache_miss
        self.response = response

    # ---- 派生指标 ----

    @property
    def input_tokens(self):
        """输入侧总 token（含未标记类型的 PROMPT_TOKEN）。"""
        return self.prompt + self.cache_hit + self.cache_miss

    @property
    def total_tokens(self):
        return self.input_tokens + self.response

    @property
    def cache_hit_rate(self):
        """缓存命中率；无缓存数据时返回 None。"""
        denom = self.cache_hit + self.cache_miss
        if denom <= 0:
            return None
        return self.cache_hit / denom

    @property
    def billable_tokens(self):
        """用于估算的计费 token（命中+未命中+输出）。"""
        return self.cache_hit + self.cache_miss + self.response

    def __add__(self, other):
        if other is None:
            return self
        return Usage(
            self.requests + other.requests,
            self.prompt + other.prompt,
            self.cache_hit + other.cache_hit,
            self.cache_miss + other.cache_miss,
            self.response + other.response,
        )

    def __repr__(self):
        return (f"<Usage req={self.requests} hit={self.cache_hit} "
                f"miss={self.cache_miss} out={self.response}>")


class Wallet:
    """某个币种的钱包状态。"""

    __slots__ = ("currency", "balance", "bonus", "total_cost")

    def __init__(self, currency, balance=Decimal(0), bonus=Decimal(0),
                 total_cost=Decimal(0)):
        self.currency = currency
        self.balance = balance
        self.bonus = bonus
        self.total_cost = total_cost

    @property
    def symbol(self):
        return {"CNY": "¥", "USD": "$"}.get(self.currency, self.currency + " ")

    def __repr__(self):
        return f"<Wallet {self.currency} {self.balance}>"


class DayUsage:
    """某一天的用量，用于画趋势。"""

    __slots__ = ("date", "usage", "cost")

    def __init__(self, day, usage, cost=None):
        self.date = day          # "YYYY-MM-DD"
        self.usage = usage
        self.cost = cost or {}   # 币种 -> Decimal


class Snapshot:
    """一次完整抓取的结果。"""

    def __init__(self):
        self.fetched_at = datetime.now()
        self.wallets = []                # [Wallet]
        self.today = Usage()             # 今日 token 用量（全局）
        self.month = Usage()             # 本月 token 用量
        self.today_by_model = {}         # model -> Usage
        self.month_by_model = {}
        self.cost_today = {}             # 币种 -> Decimal
        self.cost_month = {}
        self.daily = []                  # [DayUsage]，按日期升序
        self.elapsed_ms = 0
        self.balance_only = False        # True = 只拿到官方余额，没有用量数据
        self.warnings = []               # 局部失败时的提示

    # ---- 便利方法 ----

    def wallet(self, currency):
        for w in self.wallets:
            if w.currency == currency:
                return w
        return None

    @property
    def primary_wallet(self):
        """挑一个主钱包：有余额的优先，其次 CNY，最后第一个。"""
        for w in self.wallets:
            if w.balance > 0:
                return w
        for w in self.wallets:
            if w.currency == "CNY":
                return w
        return self.wallets[0] if self.wallets else None

    def date_string(self):
        return self.fetched_at.strftime("%Y-%m-%d %H:%M:%S")


class _UsageAccumulator:
    """把 [{model, usage:[{type, amount}]}] 累加成整体 Usage 与按模型拆解。"""

    def __init__(self):
        self.total = Usage()
        self.by_model = {}

    def feed(self, entries):
        for entry in entries or []:
            model = entry.get("model") or "unknown"
            u = Usage()
            for item in entry.get("usage") or []:
                t = item.get("type")
                v = to_int(item.get("amount"))
                if t == TYPE_REQUEST:
                    u.requests += v
                elif t == TYPE_PROMPT:
                    u.prompt += v
                elif t == TYPE_HIT:
                    u.cache_hit += v
                elif t == TYPE_MISS:
                    u.cache_miss += v
                elif t == TYPE_RESPONSE:
                    u.response += v
            if u.total_tokens or u.requests:
                self.by_model[model] = self.by_model.get(model, Usage()) + u
            self.total = self.total + u
        return self


class _CostAccumulator:
    """把费用结构累加成 {币种: Decimal}。"""

    def __init__(self):
        self.by_currency = {}
        self.per_day = {}    # "YYYY-MM-DD" -> {币种: Decimal}

    def feed_block(self, block):
        """block = {"total": [...], "days": [...], "currency": "CNY"}"""
        cur = block.get("currency") or "CNY"
        s = Decimal(0)
        for entry in block.get("total") or []:
            for item in entry.get("usage") or []:
                if item.get("type") == TYPE_REQUEST:
                    continue           # 请求数不是钱
                s += to_decimal(item.get("amount"))
        self.by_currency[cur] = self.by_currency.get(cur, Decimal(0)) + s

        for day in block.get("days") or []:
            d = day.get("date")
            if not d:
                continue
            ds = Decimal(0)
            for entry in day.get("data") or []:
                for item in entry.get("usage") or []:
                    if item.get("type") == TYPE_REQUEST:
                        continue
                    ds += to_decimal(item.get("amount"))
            slot = self.per_day.setdefault(d, {})
            slot[cur] = slot.get(cur, Decimal(0)) + ds


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _http_get_json(url, headers, timeout):
    req = urllib.request.Request(url, method="GET")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
        encoding = (e.headers.get("Content-Encoding") or "").lower() if e.headers else ""
    except urllib.error.URLError as e:
        raise NetworkError(f"网络不可达：{e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise NetworkError(f"连接失败：{e}") from e

    if encoding == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass

    text = raw.decode("utf-8", "replace")
    if status in (401, 403):
        raise AuthExpired(f"登录态被拒绝（HTTP {status}）")
    if status != 200:
        raise ApiError(f"HTTP {status}：{text[:160]}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ApiError(f"响应不是合法 JSON：{text[:160]}") from e


def _unwrap(payload):
    """拆开 {code, msg, data:{biz_code, biz_msg, biz_data}} 这层壳。"""
    if not isinstance(payload, dict):
        raise ApiError("响应结构异常")
    if payload.get("code") not in (0, None):
        raise ApiError(f"接口返回 code={payload.get('code')} msg={payload.get('msg')!r}")
    data = payload.get("data") or {}
    biz_code = data.get("biz_code", 0)
    if biz_code not in (0, None):
        biz = data.get("biz_msg") or ""
        if biz in ("INVALID_PARAM",):
            raise ApiError(f"参数不被接受：{biz}")
        if biz in ("AUTH_EXPIRED", "UNAUTHORIZED", "TOKEN_EXPIRED"):
            raise AuthExpired(f"登录态失效：{biz}")
        raise ApiError(f"业务错误 {biz_code}：{biz}")
    return data.get("biz_data")


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------

class DeepSeekClient:
    """抓取一次完整快照。线程安全（内部无共享可变状态）。"""

    def __init__(self, user_token=None, api_key=None, timeout=20):
        self.user_token = user_token
        self.api_key = api_key
        self.timeout = timeout

    # ---- 平台私有接口 ----

    def _platform_headers(self):
        if not self.user_token:
            raise AuthExpired("没有平台登录态")
        return {
            "Authorization": "Bearer " + self.user_token,
            "Accept": "application/json",
            "x-client-platform": "web",
            "User-Agent": _UA,
            "Referer": PLATFORM_BASE + "/usage",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

    def fetch_summary(self):
        url = PLATFORM_BASE + "/api/v0/users/get_user_summary"
        return _unwrap(_http_get_json(url, self._platform_headers(), self.timeout))

    def fetch_amount(self, year, month):
        url = (PLATFORM_BASE + "/api/v0/usage/amount?"
               + urllib.parse.urlencode({"month": month, "year": year}))
        return _unwrap(_http_get_json(url, self._platform_headers(), self.timeout))

    def fetch_cost(self, year, month):
        url = (PLATFORM_BASE + "/api/v0/usage/cost?"
               + urllib.parse.urlencode({"month": month, "year": year}))
        return _unwrap(_http_get_json(url, self._platform_headers(), self.timeout))

    # ---- 官方公开接口（只有余额）----

    def fetch_official_balance(self):
        if not self.api_key:
            raise AuthExpired("没有 API Key")
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Accept": "application/json",
            "User-Agent": _UA,
        }
        return _http_get_json(OFFICIAL_BALANCE_URL, headers, self.timeout)

    # ---- 组装快照 ----

    def snapshot(self, now=None):
        """抓一次快照，自动在平台接口和官方接口之间选路。

        优先用平台登录态（余额 + 用量 + 费用）。平台登录态不可用时，
        若配了 API Key 就降级为只取官方余额（``balance_only=True``）；
        两者都没有则抛 AuthExpired。
        """
        problems = []
        if self.user_token:
            try:
                return self._snapshot_platform(now)
            except AuthExpired as exc:
                problems.append(str(exc))
        if self.api_key:
            snap = self._snapshot_official(now)
            snap.warnings = problems
            return snap
        if problems:
            raise AuthExpired(problems[0])
        raise AuthExpired("既没有平台登录态，也没有配置 API Key")

    def _snapshot_platform(self, now=None):
        """走平台私有接口，拿余额 + 用量 + 费用。"""
        now = now or datetime.now()
        year, month = now.year, now.month
        t0 = time.time()
        snap = Snapshot()

        with ThreadPoolExecutor(max_workers=3) as pool:
            f_sum = pool.submit(self.fetch_summary)
            f_amt = pool.submit(self.fetch_amount, year, month)
            f_cost = pool.submit(self.fetch_cost, year, month)

            summary = f_sum.result()
            amount = f_amt.result()
            cost = f_cost.result()

        # --- 钱包 ---
        wallets = {}
        for entry in summary.get("normal_wallets") or []:
            cur = entry.get("currency") or "CNY"
            w = wallets.setdefault(cur, Wallet(cur))
            w.balance = to_decimal(entry.get("balance"))
        for entry in summary.get("bonus_wallets") or []:
            cur = entry.get("currency") or "CNY"
            w = wallets.setdefault(cur, Wallet(cur))
            w.bonus = to_decimal(entry.get("balance"))
        for entry in summary.get("total_costs") or []:
            cur = entry.get("currency") or "CNY"
            w = wallets.setdefault(cur, Wallet(cur))
            w.total_cost = to_decimal(entry.get("amount"))
        snap.wallets = [wallets[c] for c in sorted(wallets)]

        # --- 用量：total / days ---
        amt_total = _UsageAccumulator().feed((amount or {}).get("total"))
        snap.month = amt_total.total
        snap.month_by_model = amt_total.by_model

        today_str = now.strftime("%Y-%m-%d")
        day_index = {}
        for day in (amount or {}).get("days") or []:
            d = day.get("date")
            if not d:
                continue
            acc = _UsageAccumulator().feed(day.get("data"))
            day_index[d] = acc
            if d == today_str:
                snap.today = acc.total
                snap.today_by_model = acc.by_model

        # --- 费用 ---
        cost_acc = _CostAccumulator()
        blocks = cost if isinstance(cost, list) else [cost] if cost else []
        for blk in blocks:
            if isinstance(blk, dict) and "currency" in blk:
                cost_acc.feed_block(blk)
        snap.cost_month = dict(cost_acc.by_currency)
        snap.cost_today = dict(cost_acc.per_day.get(today_str, {}))

        # --- 30 天趋势 ---
        for d in sorted(day_index):
            acc = day_index[d]
            snap.daily.append(DayUsage(d, acc.total, cost_acc.per_day.get(d, {})))

        snap.elapsed_ms = int((time.time() - t0) * 1000)
        return snap

    def _snapshot_official(self, now=None):
        """只走官方公开接口取余额。拿不到用量，所以 balance_only=True。

        这条路径是给"只有 API Key、没有平台网页登录态"的用户兜底的——
        至少能看见余额，不至于什么都显示不出来。
        """
        now = now or datetime.now()
        t0 = time.time()
        data = self.fetch_official_balance()
        snap = Snapshot()
        snap.balance_only = True
        for info in data.get("balance_infos") or []:
            cur = info.get("currency") or "CNY"
            w = Wallet(cur)
            # 官方接口的 total_balance = granted + topped_up，本身就是总可用余额
            w.balance = to_decimal(info.get("total_balance"))
            w.bonus = to_decimal(info.get("granted_balance"))
            snap.wallets.append(w)
        snap.wallets.sort(key=lambda x: x.currency)
        snap.elapsed_ms = int((time.time() - t0) * 1000)
        return snap

    def check_auth(self):
        """轻量校验登录态是否还有效，返回 (ok, 说明)。"""
        try:
            data = self.fetch_summary()
        except AuthExpired as e:
            return False, str(e)
        except ApiError as e:
            return False, str(e)
        if data is None:
            return False, "响应为空"
        return True, "登录态有效"
