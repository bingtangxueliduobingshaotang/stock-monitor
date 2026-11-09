# -*- coding: utf-8 -*-
r"""
策略池监控推送脚本（GitHub Actions 部署版，自包含无需其他依赖）

用法: python stock_monitor.py [intraday|close|monitor]
  intraday - 10:30 / 14:30 盘中对策（现价/成本/浮盈亏/触发/动作+理由）
  close    - 16:00 收盘复盘 + 明日预案
  monitor  - 盘中每 10 分钟检查，仅在触发买/卖信号或关键位时推送（当天同信号去重）

配置: config.json（策略池标的、成本、关键位，随时可改）
推送: PushPlus（token 取自环境变量 PUSHPLUS_TOKEN，或 config.json 的 pushplus_token）
"""
import json
import random
import time
import os
import sys
import urllib.request
import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8"))
TOKEN = os.environ.get("PUSHPLUS_TOKEN") or CFG.get("pushplus_token", "")
POOL = CFG["pool"]
STATE_FILE = os.path.join(BASE, "state", "last_alerts.json")
BJ = datetime.timezone(datetime.timedelta(hours=8))


# ---------------- 数据层 ----------------
def http_get(url, gbk=False):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=20).read()
    return raw.decode("gbk", errors="ignore") if gbk else raw.decode("utf-8", errors="ignore")


def realtime(codes):
    """腾讯实时行情 -> {code: dict}"""
    out = {}
    raw = http_get("https://qt.gtimg.cn/q=" + ",".join(codes), gbk=True)
    for line in raw.strip().split(";"):
        if '="' not in line:
            continue
        code = line.split("=")[0].split("_")[-1]
        p = line.split('="')[1].strip('"').split("~")
        if len(p) < 40:
            continue
        try:
            out[code] = {
                "name": p[1], "price": float(p[3]), "prev": float(p[4]),
                "open": float(p[5]), "high": float(p[33]), "low": float(p[34]),
                "chg": float(p[32]),
                "vol": float(p[6]) if p[6] else 0.0,      # 手
                "amt": float(p[37]) if p[37] else 0.0,    # 万元
                "vr": float(p[49]) if len(p) > 49 and p[49] else 0.0,   # 量比
            }
        except Exception:
            continue
    return out


def kline(code, n=140):
    """日线 -> [[date,open,close,high,low,vol], ...]（腾讯为主，失败自动切新浪；盘中最后一根为动态）"""
    try:
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq"
               % (code, n))
        j = json.loads(http_get(url))
        d = j["data"][code]
        bars = d.get("qfqday") or d.get("day")
        out = [[b[0], float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])] for b in bars]
        out = [b for b in out if b[5] > 0]
        if len(out) > 60:
            return out
    except Exception:
        pass
    url = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
           "?symbol=%s&scale=240&ma=no&datalen=%d" % (code, n))
    arr = json.loads(http_get(url))
    out = []
    for it in arr:
        try:
            out.append([str(it["day"]), float(it["open"]), float(it["close"]),
                        float(it["high"]), float(it["low"]), float(it.get("volume", 0))])
        except Exception:
            continue
    return [b for b in out if b[5] > 0]


def is_trading_day():
    """A股交易日自检：上证指数实时行情的最后成交时间是否今天（自动识别节假日，零维护）
    FORCE_TRADING=1 仅供本地回归测试绕过（2026-10-24 加）"""
    if os.environ.get("FORCE_TRADING") == "1":
        return True
    try:
        raw = http_get("https://qt.gtimg.cn/q=sh000001", gbk=True)
        p = raw.split('="')[1].split("~")
        ts = p[30] if len(p) > 30 else ""
        return ts.startswith(datetime.datetime.now(BJ).strftime("%Y%m%d"))
    except Exception:
        return True


def in_window(mode, t=None):
    """有效时段守卫（北京时间）：防止延迟触发的班次在盘后乱推（误导性信号）。
    用户 2026-10-20 定（最终版）：monitor 窗口=9:15-11:30 或 13:00-15:00（A股+集合竞价，午休静默）；
    intraday 对策照旧 9:25-15:05；close: 15:00-23:59；其余一律静默。"""
    if t is None:
        _n = datetime.datetime.now(BJ)
        t = _n.hour * 100 + _n.minute
    if mode == "morning":
        return 925 <= t <= 940
    if mode == "monitor":
        return (925 <= t <= 1130) or (1300 <= t <= 1500)   # 9:25 起（20 分前可撤单，25 分才是定价）
    if mode == "intraday":
        return 925 <= t <= 1505
    if mode == "close":
        return 1500 <= t <= 1800
    return True


def market_line():
    """大盘概况一行"""
    try:
        raw = http_get("https://qt.gtimg.cn/q=sh000001,sz399001,sz399006", gbk=True)
        out = []
        for line in raw.strip().split(";"):
            if '="' not in line:
                continue
            p = line.split('="')[1].strip('"').split("~")
            out.append("%s %s（%s%%）" % (p[1], p[3], p[32]))
        return "**大盘**： " + " ｜ ".join(out) if out else ""
    except Exception:
        return ""


def ma(a, n, i):
    if i + 1 < n:
        return None
    return sum(a[i + 1 - n:i + 1]) / float(n)


# ---------------- 信号层（与手册 V4.0 / 同花顺公式同阈值） ----------------
def signals(bars):
    o = [b[1] for b in bars]
    c = [b[2] for b in bars]
    h = [b[3] for b in bars]
    l = [b[4] for b in bars]
    v = [b[5] for b in bars]
    i = len(bars) - 1
    m5, m20, m60 = ma(c, 5, i), ma(c, 20, i), ma(c, 60, i)
    m5p, m20p, m60p = ma(c, 5, i - 1), ma(c, 20, i - 1), ma(c, 60, i - 1)
    vm5p = ma(v, 5, i - 1)
    _now = datetime.datetime.now(BJ)
    _t = _now.hour * 60 + _now.minute
    if _t < 570:
        _r = 1.0   # 盘前/凌晨：按最近完整交易日算（避免量比被折算放大）
    else:
        _r = min(max((max(0, min(_t, 690) - 570) + max(0, min(_t, 900) - 780)) / 240.0, 0.05), 1.0)
    vr = (v[i] / vm5p / _r) if vm5p else 0
    if not (m5 and m20 and m60 and vm5p and m5p and m20p and m60p):
        return None
    zf = c[i] / c[i - 1] - 1
    st = h[i] - l[i]
    sy = min(o[i], c[i]) - l[i]
    sy2 = h[i] - max(o[i], c[i])
    kong = m5 < m20 < m60
    hh20p = max(h[i - 20:i])
    ll20p = min(l[i - 20:i])
    hv60 = max(v[i - 59:i + 1]) if i >= 59 else max(v[:i + 1])
    # 昨日的单针形态（供今日 B4 确认）
    sty, syy = h[i - 1] - l[i - 1], min(o[i - 1], c[i - 1]) - l[i - 1]
    prev_b4 = sty > 0 and syy / sty >= 0.5 and l[i - 1] <= m20p * 1.02 and c[i - 1] >= c[i - 2] * 0.97

    hits = []
    if zf >= 0.05 and vr >= 1.8 and c[i - 1] <= m20p < c[i]:
        hits.append(("B1", "标志性大阳线（放量+5%站上20日线）"))
    if c[i] > hh20p and vr >= 1.8:
        hits.append(("B2", "放量真突破（破20日高+量比1.8）"))
    if c[i] > hh20p and c[i - 1] > max(h[i - 21:i - 1]):
        hits.append(("B3", "两天确认（连续站上20日高）"))
    if prev_b4 and c[i] >= c[i - 1]:
        hits.append(("B4", "底分型确认（昨日单针+今日收红）"))
    if c[i] < ll20p * 0.98 and c[i - 1] >= ll20p:
        hits.append(("S1", "箱体下沿失守"))
    if c[i] < m5 and c[i - 1] >= m5p:
        hits.append(("S2", "破5日线（短线生命线）"))
    if c[i] < m20 * 0.98 and c[i - 1] >= m20p:
        hits.append(("S3", "破20日线（波段分界）"))
    if c[i] < m60 and c[i - 1] >= m60p:
        hits.append(("S4", "破60日线（中线分界）"))
    if v[i] >= hv60 and st > 0 and sy2 / st >= 0.30 and zf < 0.03:
        hits.append(("S5", "天量见顶（60日天量+长上影+滞涨）"))
    return {"hits": hits, "kong": kong, "close": c[i], "m5": m5, "m20": m20, "m60": m60,
            "zf": zf * 100, "vr": round(vr, 2), "date": bars[i][0]}


def level_status(price, lv):
    """关键位状态"""
    p = lv["price"]
    if lv["type"] == "down":
        if price < p:
            return "跌破", True
        if price <= p * 1.02:
            return "临近", False
    else:
        if price >= p:
            return "触及", True
        if price >= p * 0.98:
            return "临近", False
    return None, False


def position_layers(sg, hits, broke_half, broke_full, level_touch):
    """目标仓位（0-10 层，10=该票满配）。v2：
    基础档 空头=2（持有观察）/ 中性=4 / 多头=6；
    减半级破位 -2（下限1）；全清级破位 → 0；S类触发 -1（下限1）；B类(非空头) +3；上破 +1"""
    if not sg:
        return None, ""
    m5, m20, m60 = sg["m5"], sg["m20"], sg["m60"]
    if m5 > m20 > m60:
        base, why = 6, "多头排列"
    elif sg["kong"]:
        base, why = 2, "空头·持有观察"
    else:
        base, why = 4, "趋势中性"
    sell = any(n.startswith("S") for n, _ in hits)
    buy = any(n.startswith("B") for n, _ in hits)
    if broke_full:
        base = 0
        why += "·破全清线"
    elif broke_half:
        base = max(1, base - 2)
        why += "·破减半线"
    elif sell:
        base = max(1, base - 1)
        why += "·卖出信号"
    if buy and not sg["kong"]:
        base = min(10, base + 3)
        why += "·买入信号"
    if level_touch and base > 0:
        base = min(10, base + 1)
        why += "·上破关键位"
    return base, why


# ---------------- 推送层 ----------------
# ===== 灵活性与动态门槛（2026-10-23 用户要求："灵活一点、举一反三"）=====
# 量能口径（2026-10-23 用户纠正：不能按点位线性外推，只能跟学习者积累样本）
# 样本来源见 stock_helper/量能口径库.md；这里只放"最近一次学到的口径"
_VOL_SAMPLES = [
    # (点位档, 及格, 达标, 确认, 来源)
    (3700, 1.8, 1.9, 2.0, "老刘10-23(1.8起码/2万很好)+小寒/教主"),
]


def vol_threshold(price):
    """当前点位对应的"及格"量能（万亿）。取点位上下 150 点内最近样本；无样本则用最近档并标注"""
    if not price:
        return 1.7
    best = None
    for p, ok, good, conf, src in _VOL_SAMPLES:
        d = abs(price - p)
        if d <= 150 and (best is None or d < best[0]):
            best = (d, ok)
    if best:
        return best[1]
    # 无同档样本：用最近档，不臆造
    near = min(_VOL_SAMPLES, key=lambda x: abs(price - x[0]))
    return near[1]


def vol_ref(price):
    """返回 (及格, 达标, 确认, 来源, 是否同档) —— 推送文案直接引用，不自己算"""
    if not price:
        return 1.7, 1.8, 1.9, "默认", False
    for p, ok, good, conf, src in _VOL_SAMPLES:
        if abs(price - p) <= 150:
            return ok, good, conf, src, True
    near = min(_VOL_SAMPLES, key=lambda x: abs(price - x[0]))
    return near[1], near[2], near[3], near[4], False


_CACHE = {}


def _cached(key, ttl, fn):
    """简单缓存（同一次运行内避免重复网络请求，2026-10-25 加）"""
    import time as _t
    now = _t.time()
    v = _CACHE.get(key)
    if v and now - v[0] < ttl:
        return v[1]
    r = fn()
    _CACHE[key] = (now, r)
    return r


def market_amount():
    return _cached("amount", 55, _market_amount_raw)


def _market_amount_raw():
    """两市成交额（万亿）"""
    try:
        raw = http_get("https://qt.gtimg.cn/q=sh000001,sz399001", gbk=True)
        amt = 0.0
        for line in raw.strip().split(";"):
            if '="' not in line:
                continue
            p = line.split('="')[1].strip('"').split("~")
            if len(p) > 37:
                try:
                    amt += float(p[37]) * 10000
                except Exception:
                    pass
        return amt / 1000000000000.0 if amt else None
    except Exception:
        return None


# ===== 市场状态机（2026-10-23：七阶段 + 迁移条件）=====
REGIME = {
    1: ("单边下跌·阴跌段", "不猜底，等极端", 2),
    2: ("单边下跌·极端段", "看情绪和缺口，第 3 个缺口才动手", 2),
    3: ("情绪反扑", "敢上，但打一波就走", 6),
    4: ("反弹修复", "回踩买，破 5 日线减", 7),
    5: ("上涨趋势·健康", "拿住，别乱动", 10),
    6: ("上涨趋势·过热", "分批止盈，不追", 5),
    7: ("高位做头/滞涨", "清仓，等下一轮", 3),
}


# ===== 反趋势特例（2026-10-23 用户：指数空头但个别票很强，这种特例能赚大钱）=====
def reverse_strong(code):
    """逆势走强判定：指数近 10 日跌 ≥3%，该票近 10 日不跌（≥0%），且站在 5 日线上
    返回 (是否成立, 说明)"""
    try:
        idx = kline("sh000001", 16)
        ks = kline(code, 16)
        if len(idx) < 11 or len(ks) < 11:
            return False, ""
        idx_ret = (idx[-1][2] / idx[-11][2] - 1) * 100
        stk_ret = (ks[-1][2] / ks[-11][2] - 1) * 100
        ma5 = sum(b[2] for b in ks[-5:]) / 5.0
        price = ks[-1][2]
        if idx_ret <= -3 and stk_ret >= 0 and price > ma5:
            return True, "指数10日 %+.1f%%，该票 %+.1f%%（逆势）且站上5日线" % (idx_ret, stk_ret)
        return False, ""
    except Exception:
        return False, ""


# ===== 量能节奏预警（2026-10-23 用户问：量以后会预警吗）=====
# ===== 分钟级脉冲放量（用户 2026-10-23：最重要的是一两分钟内的快速放量）=====
def minute_bars(code="sh000001"):
    """抓当日分时：[(HHMM, 价格, 每分钟成交量股, 每分钟成交额元)]"""
    try:
        raw = http_get("https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=" + code)
        j = json.loads(raw)
        arr = j["data"][code]["data"]["data"]
        out, prev_v, prev_a = [], 0.0, 0.0
        for row in arr:
            p = row.split()
            if len(p) < 4:
                continue
            t, price, cum_v, cum_a = p[0], float(p[1]), float(p[2]), float(p[3])
            if t > "1500":                     # 排除收盘集合竞价
                break
            out.append((t, price, cum_v - prev_v, cum_a - prev_a))
            prev_v, prev_a = cum_v, cum_a
        return out
    except Exception:
        return []


# ===== 龙头异动（2026-10-23 用户：宁王/华天/中信这些龙头的异动也是信号）=====
LEADERS = [
    # 权重/指数锚
    ("sz300750", "宁德时代", "创业板+新能源锚"),
    ("sh600519", "贵州茅台", "白酒/消费"),
    ("sh601318", "中国平安", "保险/大金融"),
    ("sh600036", "招商银行", "银行"),
    # 成长主线
    ("sh603259", "药明康德", "创新药/CXO"),
    ("sh600276", "恒瑞医药", "创新药/龙头"),
    ("sz002371", "北方华创", "半导体设备"),
    ("sz002185", "华天科技", "半导体封测"),
    ("sz300502", "新易盛", "光模块"),
    ("sz300308", "中际旭创", "光模块/算力"),
    ("sh601138", "工业富联", "AI服务器"),
    # 机器人/新能源
    ("sz300124", "汇川技术", "机器人/工控"),
    ("sz002050", "三花智控", "机器人/汽零"),
    ("sh601689", "拓普集团", "机器人/汽零"),
    ("sz002594", "比亚迪", "新能源车"),
    # 金融/周期/其他
    ("sh600030", "中信证券", "券商旗手"),
    ("sz300059", "东方财富", "券商/散户情绪"),
    ("sh601899", "紫金矿业", "有色/黄金"),
    ("sh601088", "中国神华", "煤炭/红利"),
    ("sh600031", "三一重工", "工程机械"),
]


def leader_move(lookback=12, mult=8.0, spike=3.0):
    """龙头异动：最近 lookback 分钟内 ①单分钟额 ≥ 前20分钟均额 mult 倍 或 ②5 分钟涨跌 ≥ spike%
    返回 [(名称, 方向, 说明, 时刻, 倍数, 该分钟额亿, 5分钟涨跌%)]"""
    out = []
    for code, name, tag in LEADERS:
        try:
            bars = minute_bars(code)
            if len(bars) < 25:
                continue
            n = len(bars)
            start = max(20, n - lookback)
            for i in range(start, n):
                base_amt = sum(b[3] for b in bars[i - 20:i]) / 20.0
                if base_amt <= 0:
                    continue
                ratio = bars[i][3] / base_amt
                chg5 = 0.0
                if i >= 5 and bars[i - 5][1]:
                    chg5 = (bars[i][1] / bars[i - 5][1] - 1) * 100
                if ratio >= mult or abs(chg5) >= spike:
                    direction = "拉升" if chg5 >= 0 else "跳水"
                    out.append((name, direction, tag, "%s:%s" % (bars[i][0][:2], bars[i][0][2:]),
                                round(ratio, 1), bars[i][3] / 1e8, round(chg5, 2)))
                    break
        except Exception:
            continue
    return out


def volume_pulse(lookback=12, mult=3.5):
    """检测最近 lookback 分钟内的脉冲：单分钟量 ≥ 前 20 分钟均量 mult 倍，或连续 2 分钟 ≥ 2.5 倍
    返回 [(HH:MM, 倍数, 该分钟成交额亿, 价格, 当分钟涨跌%)]"""
    bars = minute_bars()
    if len(bars) < 25:
        return []
    hits = []
    n = len(bars)
    start = max(20, n - lookback)
    for i in range(start, n):
        base_slice = bars[i - 20:i]
        base = sum(b[2] for b in base_slice) / 20.0
        if base <= 0:
            continue
        cur = bars[i][2]
        ratio = cur / base
        prev_price = bars[i - 1][1]
        chg = (bars[i][1] / prev_price - 1) * 100 if prev_price else 0
        ok = ratio >= mult
        if not ok and i > 0:
            prev_ratio = bars[i - 1][2] / base
            ok = (ratio >= 2.5 and prev_ratio >= 2.5)      # 连续两分钟
        if ok:
            hits.append(("%s:%s" % (bars[i][0][:2], bars[i][0][2:]),
                         round(ratio, 1), bars[i][3] / 1e8, bars[i][1], round(chg, 2)))
    return hits


# ===== 策略池量能同比 + 板块对比（2026-10-23 用户要求）=====
# 板块篮子：策略池标的 → (板块名, [同板块代表股3只], 龙头)
# 用同板块多只代表股的等权均值作为"板块涨跌"（东财板块接口不稳定，2026-10-25 改）
# 开盘分歧判定用的**板块参照标的**（2026-11-09 用户实例催生）
# 为什么需要：2026-11-05 上证低开 -1.07%、科创50 低开 -2.33%、588710 低开 -3.66%，
# 但 159516 却高开 +1.9% —— 按个股判会得出"高开不能买"，按板块判是"大分歧可打底仓"，结论相反。
# 规则：个股"一致(高开)"但板块"分歧(低开)" → 以板块为准，标 🟢(板块)
SECTOR_ETF_REF = {
    "sz159516": ("sh588710", "科创半导体"),     # 半导体设备ETF → 科创半导体ETF
}

SECTOR_BASKET = {
    "sz000425": ("工程机械", ["sh600031", "sz000528", "sz000157"], "sh600031"),   # 三一/柳工/中联，龙头三一
    "sh600469": ("轮胎汽零", ["sh601966", "sh601058", "sz002984"], "sh601966"),   # 玲珑/赛轮/森麒麟
    "sh520700": ("创新药", ["sh600276", "sh603259", "sh688235"], "sh600276"),     # 恒瑞/药明/百济
    "sz159516": ("半导体设备", ["sz002371", "sh688012", "sh688072"], "sz002371"), # 北方华创/中微/拓荆
    "sh512880": ("券商", ["sh600030", "sz300059", "sh601688"], "sh600030"),       # 中信/东财/华泰
    "sz159559": ("机器人", ["sz300124", "sz002050", "sh601689"], "sz300124"),     # 汇川/三花/拓普
    "sz002738": ("有色锂", ["sh601899", "sz002460", "sz002466"], "sh601899"),     # 紫金/赣锋/天齐
}


def _progress():
    """已过交易时间占比"""
    now = datetime.datetime.now(BJ)
    hm = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= hm <= 11 * 60 + 30:
        p = hm - (9 * 60 + 30)
    elif 13 * 60 <= hm <= 15 * 60:
        p = 120 + (hm - 13 * 60)
    elif hm > 15 * 60:
        p = 240
    else:
        p = 1
    return max(1, min(240, p)) / 240.0


def pool_volume_pace():
    """策略池 7 只：今日额、较昨日同期增减%、预计全天额
    口径：今日量(手)×100 股 / (昨日全天量(股) × 进度)"""
    prog = _progress()
    codes = [i["code"] for i in POOL]
    rt = realtime(codes)
    rows = []
    for item in POOL:
        code, name = item["code"], item["name"]
        r = rt.get(code)
        if not r:
            continue
        try:
            ks = kline(code, 6)
            if len(ks) < 2:
                continue
            yest_shares = ks[-2][5]
            today_shares = r["vol"] * 100
            ratio = today_shares / (yest_shares * prog) if yest_shares and prog else None
            amt_yi = r["amt"] / 10000.0                    # 万元 → 亿元
            est = amt_yi / prog if prog > 0 else None
            rows.append((name, amt_yi, (ratio - 1) * 100 if ratio else None, est))
        except Exception:
            continue
    return rows


def pool_vs_peer():
    """策略池三层对比（用户 2026-10-25 要求）：个股 vs 所属板块均值 vs 板块龙头
    返回 [(名称, 个股chg, 板块名, 板块chg, 龙头名, 龙头chg, 结论)]"""
    need = list(dict.fromkeys([i["code"] for i in POOL] +
                             [c for _, (_, basket, ld) in SECTOR_BASKET.items() for c in basket] +
                             [ld for _, (_, _, ld) in SECTOR_BASKET.items()]))
    rt = {}
    for i in range(0, len(need), 40):
        rt.update(realtime(need[i:i + 40]))
    out = []
    for item in POOL:
        code, name = item["code"], item["name"]
        pack = SECTOR_BASKET.get(code)
        if not pack or code not in rt:
            continue
        sector, basket, leader = pack
        chgs = [rt[c]["chg"] for c in basket if c in rt]
        if not chgs:
            continue
        s_chg = sum(chgs) / len(chgs)
        l_chg = rt[leader]["chg"] if leader in rt else None
        c_chg = rt[code]["chg"]
        vs_sector = c_chg - s_chg
        if vs_sector >= 1.0 and (l_chg is None or c_chg >= l_chg):
            verdict = "**板块内最强**（强于板块 %.1f 个点，不弱于龙头）" % vs_sector
        elif vs_sector >= 1.0:
            verdict = "**强于板块** %.1f 个点，但龙头更强" % vs_sector
        elif vs_sector <= -1.0:
            verdict = "**弱于板块** %.1f 个点（拖后腿）" % abs(vs_sector)
        else:
            verdict = "跟随板块"
        out.append((name, c_chg, sector, s_chg,
                    (rt[leader]["name"] if leader in rt else "龙头"), l_chg, verdict))
    return out


def day_progress():
    """当前时刻的「已成交量占比」（A股日内分布曲线，非时间进度）"""
    now = datetime.datetime.now(BJ)
    hm = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= hm <= 11 * 60 + 30:
        passed = hm - (9 * 60 + 30)
    elif 13 * 60 <= hm <= 15 * 60:
        passed = 120 + (hm - 13 * 60)
    elif hm > 15 * 60:
        passed = 240
    else:
        passed = 1
    passed = max(1, min(240, passed))
    _CURVE = [(15, 0.11), (30, 0.22), (45, 0.29), (60, 0.38), (75, 0.42),
              (90, 0.46), (105, 0.50), (120, 0.55), (135, 0.60), (150, 0.65),
              (165, 0.70), (180, 0.76), (195, 0.82), (210, 0.88), (225, 0.94), (240, 1.0)]
    for _m, _p in _CURVE:
        if passed <= _m:
            return _p
    return 1.0


def vs_yesterday(code, r=None):
    """今日累计量 vs 昨日同期（昨日全天量 × 日内占比）→ 放量/缩量百分比
    返回 (pct, 昨日同期量说明) ；pct>0 放量，<0 缩量"""
    try:
        r = r or (realtime([code]).get(code) or {})
        today_shares = (r.get("vol") or 0) * 100
        if not today_shares:
            return None
        ks = kline(code, 8)
        _today = datetime.datetime.now(BJ).strftime("%Y-%m-%d")
        hist = [b for b in ks if b[0] != _today]
        if not hist:
            return None
        yest = hist[-1][5]
        prog = day_progress()
        expect = yest * prog
        if expect <= 0:
            return None
        return (today_shares / expect - 1) * 100
    except Exception:
        return None


def vol_pace():
    """当前量能 vs 过去 5 日"同进度"量能，返回 (比值, 当前万亿, 推算全天万亿, 进度%)
    盘中任意时刻可用：已过交易分钟 / 240 作为进度"""
    try:
        now = datetime.datetime.now(BJ)
        hm = now.hour * 60 + now.minute
        # 交易分钟：9:30-11:30 = 120 分钟；13:00-15:00 = 120 分钟
        if 9 * 60 + 30 <= hm <= 11 * 60 + 30:
            passed = hm - (9 * 60 + 30)
        elif 13 * 60 <= hm <= 15 * 60:
            passed = 120 + (hm - 13 * 60)
        elif hm > 15 * 60:
            passed = 240
        else:
            passed = 1
        passed = max(1, min(240, passed))
        # 日内成交量分布（A股经验：开盘/尾盘集中，非均匀）
        # 累计占比表：分钟数 → 已成交量占比
        _CURVE = [(15, 0.11), (30, 0.22), (45, 0.29), (60, 0.38), (75, 0.42),
                  (90, 0.46), (105, 0.50), (120, 0.55), (135, 0.60), (150, 0.65),
                  (165, 0.70), (180, 0.76), (195, 0.82), (210, 0.88), (225, 0.94), (240, 1.0)]
        prog = 0.01
        for _m, _p in _CURVE:
            if passed <= _m:
                prog = _p
                break
        else:
            prog = 1.0
        amt = market_amount()
        if not amt:
            return None, None, None, prog * 100
        # 今日量用实时成交量（kline 盘中不含今日，2026-10-26 修正）
        rts = realtime(["sh000001"]).get("sh000001") or {}
        today_shares = (rts.get("vol") or 0) * 100
        ks = kline("sh000001", 8)
        _today = datetime.datetime.now(BJ).strftime("%Y-%m-%d")
        hist = [b for b in ks if b[0] != _today][-5:]
        if not hist or not today_shares:
            return None, amt, None, prog * 100
        avg_vol = sum(b[5] for b in hist) / float(len(hist))
        expect = avg_vol * prog
        ratio = today_shares / expect if expect > 0 else None
        return ratio, amt, amt / prog if prog > 0 else None, prog * 100
    except Exception:
        return None, None, None, 0


# ===== 通知分级（2026-10-23 用户问：怎么优化通知）=====
P0_TAGS = ("regime", "sent|ice", "sent|hot", "nt", "gap", "pt", "volpace", "adv", "pulse", "leader")
P2_TAGS = ("broker", "cyb", "ningde", "small", "bank", "vol|tier", "sent|sector")


def alert_level(tag):
    """P0=决策级（必推）；P2=参考级（限额）；其余 P1=执行级"""
    t = tag or ""
    if t in P0_TAGS or t.startswith(P0_TAGS):
        return "P0"
    if t in P2_TAGS or t.startswith(P2_TAGS):
        return "P2"
    return "P1"


def attack_advice():
    """大盘状态 + 攻防建议（用户 2026-10-23：预警里每天要有"该防守还是进攻"）"""
    try:
        n, name, tip, pos, ev = regime()
        if not n:
            return ""
        if pos <= 3:
            mode, adv = "防守", "防守为主：不加仓、不追高；有票的按关键位减，空仓的等信号"
        elif pos <= 6:
            mode, adv = "试探进攻", "可试探进攻：分批上、单票不重仓；涨一波先减，不恋战"
        else:
            mode, adv = "进攻", "进攻：趋势成立，回踩就买、拿住不动；只在跌破关键位时减"
        return "**📍 大盘状态：%d·%s（%s）**｜仓位上限 %d 成\n> 今日对策：%s" % (n, name, mode, pos, adv)
    except Exception:
        return ""


def vp_tag(r, code=None):
    """量价判读（用户 2026-10-25：不能只看涨幅，要看量比）
    返回 (标签, 提示)"""
    try:
        chg = r.get("chg", 0.0)
        vr = r.get("vr", 0.0)
        if not vr:
            return "", ""
        if chg > 0 and vr >= 2.0:
            return "放量上涨", "量价配合（强，可持续）"
        if chg > 0 and vr <= 0.6:
            return "缩量上涨", "**量价背离：虚涨，随时回落**"
        if chg < 0 and vr >= 2.0:
            return "放量下跌", "**有资金在出，别接**"
        if chg < 0 and vr <= 0.6:
            return "缩量下跌", "抛压不大，可能是洗盘"
        if abs(chg) < 0.3 and vr >= 1.8:
            return "放量滞涨", "**量放着但涨不动 = 有人在派**"
        return "", ""
    except Exception:
        return "", ""


def structure_signal():
    """底部结构识别（2026-11-09 自建：只看价格与量能，不复述任何人的话）
    launch = 放量启动后回踩中（买点）；hold = 启动后走强；none = 无结构
    """
    try:
        ks = kline("sh000001", 40)
        if len(ks) < 25:
            return "none", ""
        lows = [float(b[4]) for b in ks]
        closes = [float(b[2]) for b in ks]
        vols = [float(b[5]) for b in ks]
        n = len(ks)
        win_low = min(lows[-20:])
        tests = sum(1 for x in lows[-20:] if x <= win_low * 1.015)
        launch_i = None
        for i in range(n - 12, n):
            if i < 6:
                continue
            v5 = sum(vols[i-5:i]) / 5.0
            chg = (closes[i] / closes[i-1] - 1) * 100 if closes[i-1] else 0
            if vols[i] > v5 * 1.15 and chg > 1.5:
                launch_i = i
        if tests < 2 or launch_i is None:
            return "none", ""
        dev = (closes[-1] / closes[launch_i] - 1) * 100
        detail = "近20日%d次探%.0f不破｜放量启动收%.0f｜现价距启动%+.1f%%" % (tests, win_low, closes[launch_i], dev)
        if dev > 2:
            return "hold", detail
        if dev > -3:
            return "launch", detail
        return "none", detail
    except Exception:
        return "none", ""


def regime():
    """返回 (阶段号, 名称, 一句话, 仓位上限成数, 判定证据)"""
    try:
        sh = realtime(["sh000001"]).get("sh000001")
        ks = kline("sh000001", 30)
        if not sh or len(ks) < 21:
            return 0, "未知", "", 0, ""
        closes = [b[2] for b in ks]
        _st, _dt = structure_signal()
        if _st == "hold":
            return 7, "反弹确认·走强", "放量启动后已站上启动位", 7, _dt
        if _st == "launch":
            _m5 = sum(closes[-5:]) / 5.0
            _pos = "站在5日线" if sh["price"] >= _m5 else "略破5日线"
            return 6, "反弹确认·回踩", "放量启动后回踩，" + _pos + "；回踩是买点不是卖点", 5, _dt
        ma5 = sum(closes[-5:]) / 5.0
        ma10 = sum(closes[-10:]) / 10.0
        ma20 = sum(closes[-20:]) / 20.0
        c = sh["price"]
        amt = market_amount() or 0
        se = sentiment()
        zt, dt, lb = se.get("zt"), se.get("dt"), se.get("max_lb", 0)
        gn, _ = gap_rule()
        # 连涨连跌
        up_days = down_days = 0
        for i in range(len(ks) - 1, 0, -1):
            if ks[i][2] > ks[i - 1][2]:
                if down_days:
                    break
                up_days += 1
            elif ks[i][2] < ks[i - 1][2]:
                if up_days:
                    break
                down_days += 1
            else:
                break
        # 5 日跌幅
        d5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else 0
        # 放量滞涨
        stall = False
        if len(ks) >= 2 and ks[-2][5] > 0:
            stall = (ks[-1][5] / ks[-2][5] - 1 >= 0.30) and sh["chg"] < 0.2
        ev = "MA5 %.0f/MA10 %.0f/MA20 %.0f｜量 %.2f万亿｜涨停 %s/跌停 %s/连板 %s｜连涨%d连跌%d｜5日%+.1f%%" % (
            ma5, ma10, ma20, amt, zt, dt, lb, up_days, down_days, d5)
        # —— 判定（从极端到常规）——
        if zt is not None and (zt >= 100 or lb >= 7 or up_days >= 5):
            n = 6
        elif stall and c > ma20:
            n = 7
        elif (dt is not None and dt >= 40) or gn == 3 or d5 <= -6:
            # 极端段；若同时"放量达标 + 当日拉起" → 反扑（第 3 缺口/冰点都算极端触发）
            _need = vol_threshold(c)
            if amt and amt >= _need and sh["chg"] >= 0.3:
                n = 3
            else:
                n = 2
        elif amt and sh["chg"] >= 0.3 and (dt is None or dt < 40) and c > ma5 and up_days >= 1 and ma5 < ma10 < ma20:
            n = 4
        elif ma5 > ma10 > ma20 and c > ma20:
            n = 5
        elif c < ma20 and ma5 < ma10 < ma20:
            n = 1
        else:
            n = 4 if c > ma5 else 1
        name, tip, pos = REGIME[n]
        return n, name, tip, pos, ev
    except Exception:
        return 0, "未知", "", 0, ""


def market_mode():
    """市场状态（2026-10-23 用户校正）：
    strong  = 趋势级强势（量达标 + 指数走强 + 不是从冰点刚爬起来）
    rebound = 情绪反扑（冰点/连跌之后放量拉起）——**这时候不必要求均线多头**，
              因为连续大跌后本来必然空头排列；但要做的是"反弹交易"而非"趋势交易"
    weak    = 缩量下跌
    normal  = 其它
    """
    try:
        rs = realtime(["sh000001", "sz399006"])
        sh, cy = rs.get("sh000001"), rs.get("sz399006")
        amt = market_amount()
        need = vol_threshold(sh["price"]) if sh else 1.7
        if not sh or amt is None:
            return "normal", ""
        # 情绪冰点判定（跌停多 / 涨停少 / 当天曾大幅下探后拉回）
        _se = {}
        try:
            _se = sentiment()
        except Exception:
            pass
        _zt, _dt = _se.get("zt"), _se.get("dt")
        _ice = (_dt is not None and _dt >= 40) or (_zt is not None and _zt <= 20)
        _big_dip = bool(sh.get("low") and sh.get("prev") and sh["low"] <= sh["prev"] * 0.985)   # 盘中曾跌 1.5%
        if (amt >= need) and sh["chg"] >= 0.3 and (cy and cy["chg"] >= 0.5):
            if _ice or _big_dip:
                return "rebound", "情绪反扑（量 %.2f 万亿达标，但今天是从冰点/大跌里拉起来的）" % amt
            return "strong", "趋势级强势（量 %.2f 万亿 ≥ 所需 %.2f，上证 %+.2f%%）" % (amt, need, sh["chg"])
        if (amt < need * 0.8) and sh["chg"] <= -0.3:
            return "weak", "缩量下跌（量能不足所需 %.2f 万亿）" % need
        return "normal", ""
    except Exception:
        return "normal", ""


def market_surge():
    """全市场同步异动（用户 10-23：今天 13:00 一开盘所有板块一起往上拉，这种特殊要播报）"""
    try:
        codes = ["sh000001", "sz399001", "sz399006", "sh000688", "sh000852"]
        rs = realtime(codes)
        up = [(c, r) for c, r in rs.items() if r and r["chg"] >= 0.4]
        if len(up) >= 4:
            seg = " ｜ ".join("%s %+.2f%%" % (NAMES.get(c, c), r["chg"]) for c, r in up[:4])
            return seg, len(up)
        return None, 0
    except Exception:
        return None, 0


# ===== 国家队入场监测（2026-10-23 用户要求：宽基 ETF 突然放量 = 国家队在买）=====
ETF_WATCH = [("sh510300", "沪深300ETF"), ("sh510050", "上证50ETF"), ("sh510500", "中证500ETF"),
             ("sz159915", "创业板ETF"), ("sh588000", "科创50ETF")]


def etf_flow():
    """返回 [(名称, 今日成交额亿, 倍数, 涨跌%)] —— 倍数 = 今日额 / 近5日均额"""
    out = []
    try:
        raw = http_get("https://qt.gtimg.cn/q=" + ",".join(c for c, _ in ETF_WATCH), gbk=True)
        today = {}
        for line in raw.strip().split(";"):
            if '="' not in line:
                continue
            p = line.split('="')[1].strip('"').split("~")
            if len(p) > 37:
                code = ("sh" if p[2].startswith(("5", "6")) else "sz") + p[2]
                try:
                    today[code] = (float(p[37]) / 10000.0, float(p[32]) if p[32] else 0.0)
                except Exception:
                    pass
        for code, name in ETF_WATCH:
            if code not in today:
                continue
            amt, chg = today[code]
            try:
                ks = kline(code, 12)
                hist = ks[:-1][-5:]
                if len(hist) >= 5:
                    avg = sum(b[5] * (b[2] + b[3] + b[4]) / 3 / 1e8 for b in hist) / 5.0  # 量(股)×均价→元→亿
                    mult = amt / avg if avg > 0 else 0
                else:
                    mult = 0
            except Exception:
                mult = 0
            out.append((name, amt, mult, chg))
    except Exception:
        pass
    return out


def national_team():
    """两级判定（用户 10-23：突然放量就要报）：
    confirm = ≥2 只宽基 ETF 达近 5 日均额 2.5 倍 → 疑似国家队真金白银进场
    warn    = ≥2 只达 1.8 倍，且指数当日由跌转涨/大涨 → 资金明显异动（先预警）
    """
    rows = etf_flow()
    strong = [r for r in rows if r[2] >= 2.5]
    mid = [r for r in rows if r[2] >= 1.8]
    sh = None
    try:
        sh = realtime(["sh000001"]).get("sh000001")
    except Exception:
        pass
    up = bool(sh and sh.get("chg", 0) >= 0.3)
    if len(strong) >= 2:
        seg = " ｜ ".join("%s %.0f亿(%.1f倍)" % (n, a, m) for n, a, m, c in strong[:4])
        return "confirm", seg, rows
    if len(mid) >= 2 and up:
        seg = " ｜ ".join("%s %.0f亿(%.1f倍)" % (n, a, m) for n, a, m, c in mid[:4])
        return "warn", seg, rows
    return None, None, rows


# ===== 跳空缺口计数（周老师"缺口三定律"：第1个逃、第2个等、第3个加）=====
def gap_count(days=20):
    """统计【当前这一波下跌】里的向下缺口（用户 2026-10-23 校正口径）：
    - 只看最近 20 个交易日
    - 严格缺口（今高<昨低）或开盘跳空 ≥0.5%
    - **波段重置**：中途出现单日涨 ≥3%（大反弹）就清空重数
    返回 [(日期, 缺口点数, 类型)]
    """
    out = []
    try:
        ks = kline("sh000001", days + 10)
        seg = ks[-days:] if len(ks) > days else ks
        prev_low = None
        for i in range(1, len(seg)):
            prev, cur = seg[i - 1], seg[i]
            if prev_low is None:
                prev_low = prev[4]
            # 大反弹 → 波段重置
            if prev[2] and cur[2] / prev[2] - 1 >= 0.03:
                out = []
                prev_low = cur[4]
                continue
            _min_gap = prev_low * 0.003          # 只算 ≥0.3% 的缺口（用户 10-23：小的不算，数出来才对）
            if cur[3] < prev_low and (prev_low - cur[3]) >= _min_gap:
                out.append((cur[0], round(prev_low - cur[3], 1), "严格"))
            elif cur[1] < prev_low * 0.995 and (prev_low - cur[1]) >= _min_gap:
                out.append((cur[0], round(prev_low - cur[1], 1), "开盘"))
            prev_low = cur[4]
    except Exception:
        pass
    return out


def gap_rule():
    """返回 (第几个, 提示语) —— 最近一个缺口在整段里的序号"""
    gs = gap_count()
    if not gs:
        return None, None
    n = len(gs)
    tip = ("第 1 个 → **要逃**（破位缺口，不接）" if n == 1 else
           "第 2 个 → **继续等**（加速缺口，还有低点）" if n == 2 else
           "第 3 个 → **可以加**（衰竭缺口，抄底时机）" if n == 3 else
           "第 %d 个 → 已超过三个，规律失效，按情绪/资金判断" % n)
    return n, tip


# ===== 止盈 / 不参与（2026-10-23 用户要求：行情好要提醒止盈，末期要提醒别追）=====
def profit_signal():
    """返回 [(级别, 文案)] —— 止盈与"别参与"的提醒
    A. 情绪过热（涨停≥100 或 连板≥7）→ 分批止盈
    B. 放量滞涨（量增≥30% 但指数涨幅<0.2%）→ 钱在出
    C. 指数连涨≥4 天 → 逢高减
    D. 极端过热（涨停≥120 且 连板≥8）→ 别参与
    """
    out = []
    try:
        sh = realtime(["sh000001"]).get("sh000001")
        amt = market_amount()
        se = sentiment()
        zt, lb = se.get("zt"), se.get("max_lb", 0)
        # 指数连涨天数
        days_up = 0
        try:
            ks = kline("sh000001", 12)
            for i in range(len(ks) - 1, 0, -1):
                if ks[i][2] > ks[i - 1][2]:
                    days_up += 1
                else:
                    break
        except Exception:
            pass
        vol_grow = None
        try:
            ks2 = kline("sh000001", 6)
            if len(ks2) >= 2 and ks2[-2][5] > 0:
                vol_grow = ks2[-1][5] / ks2[-2][5] - 1
        except Exception:
            pass
        if zt is not None and (zt >= 100 or lb >= 7):
            out.append(("hot", "🔥 【情绪过热】涨停 %d 家 / 最高 %d 连板 → **分批止盈**：涨得最猛的先减 1/3（s2）" % (zt, lb)))
        if vol_grow is not None and sh and vol_grow >= 0.30 and sh["chg"] < 0.2:
            grow = vol_grow
            if True:
                out.append(("stall", "⚠️ 【放量滞涨】量增 %.0f%% 但指数只涨 %.2f%% → **钱在出，不是钱在进**：逢高兑现，别加仓" % (grow * 100, sh["chg"])))
        if days_up >= 4:
            out.append(("run", "📈 【连涨 %d 天】→ **逢高减一档**：连涨越久越要防回踩（s2）" % days_up))
        if zt is not None and zt >= 120 and lb >= 8:
            out.append(("avoid", "🚫 【极端过热】涨停 %d 家 / %d 连板 → **这种时候不要参与**：热闹是给接盘的人看的，等回落再说" % (zt, lb)))
    except Exception:
        pass
    return out


def sentiment():
    """情绪指标（2026-10-23 建）：涨停/跌停家数 + 最高连板 + 涨停板块集中度
    数据源：东财涨停池/跌停池（push2ex），一次请求拿全"""
    import collections
    ut = "7eea3edcaed734bea9cbfc24409ed989"
    d = datetime.datetime.now(BJ).strftime("%Y%m%d")
    out = {"zt": None, "dt": None, "max_lb": 0, "top_sector": None, "top_sector_n": 0}
    try:
        u = ("https://push2ex.eastmoney.com/getTopicZTPool?ut=%s&dpt=wz.ztzt"
             "&Pageindex=0&pagesize=200&sort=fbt%%3Aasc&date=%s&_=%d"
             % (ut, d, int(datetime.datetime.now().timestamp() * 1000)))
        j = json.loads(http_get(u))
        dd = j.get("data") or {}
        out["zt"] = dd.get("tc")
        pool = dd.get("pool") or []
        out["max_lb"] = max([x.get("lbc") or 0 for x in pool] or [0])
        cnt = collections.Counter([x.get("hybk") for x in pool if x.get("hybk")])
        if cnt:
            sec, n = cnt.most_common(1)[0]
            out["top_sector"], out["top_sector_n"] = sec, n
    except Exception:
        pass
    try:
        u = ("https://push2ex.eastmoney.com/getTopicDTPool?ut=%s&dpt=wz.ztzt"
             "&Pageindex=0&pagesize=200&sort=fund%%3Aasc&date=%s&_=%d"
             % (ut, d, int(datetime.datetime.now().timestamp() * 1000)))
        j = json.loads(http_get(u))
        out["dt"] = (j.get("data") or {}).get("tc")
    except Exception:
        pass
    return out


def _ref_ok(state, limit=2):
    """参考类信号（银行/券商/创业板/宁德/中证1000）当日降噪：每天最多 limit 条（用户 2026-10-21 定）"""
    n = state.get("ref_cnt", 0)
    if n >= limit:
        return False
    state["ref_cnt"] = n + 1
    return True


def save_archive(kind, content):
    """复盘存档（用户 2026-10-21 要求）。
    方案：①本地写 复盘归档/日期_类型.md ②在 Actions 里把正文打进日志（带标记，日志保留 90 天可回捞）。
    注：直接写 repo 需要 workflow 写权限，当前 PAT 无该权限，故用此方案。"""
    day = datetime.datetime.now(BJ).strftime("%Y-%m-%d")
    fn = "%s_%s.md" % (day, kind)
    try:
        d = os.path.join(os.path.dirname(os.path.dirname(STATE_FILE)), "复盘归档")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
            f.write("# %s %s\n\n%s\n" % (day, kind, content))
        print("ARCHIVE LOCAL OK:", fn)
    except Exception as e:
        print("ARCHIVE LOCAL FAIL:", str(e)[:60])
    print("===ARCHIVE-BODY-BEGIN %s===" % fn)
    print(content)
    print("===ARCHIVE-BODY-END %s===" % fn)


def push(title, content):
    if not TOKEN:
        print("[WARN] 未配置 PUSHPLUS_TOKEN，跳过推送")
        return
    st = load_state()
    st["count"] = st.get("count", 0) + 1
    # 2026-10-21 用户反馈：删除"上一条可能未送达"提示（会造成"以为没推"的误判）。
    # 次数与时间仍记入 push_log，需要核对时查日志即可。
    st["last_push_ts"] = datetime.datetime.now(BJ).isoformat()
    log = st.get("push_log") or []
    st["push_log"] = (log + [{"ts": datetime.datetime.now(BJ).isoformat(timespec="seconds"),
                              "time": datetime.datetime.now(BJ).strftime("%H:%M"),
                              "title": title, "brief": content.split("\n")[0][:80]}])[-5:]
    save_state(st)
    content += "\n\n---\n\n📊 本月第 %d 次推送 ｜ PushPlus 免费额度充足" % st["count"]
    body = json.dumps({"token": TOKEN, "title": title, "content": content,
                       "template": "markdown"}).encode("utf-8")
    req = urllib.request.Request("https://www.pushplus.plus/send", data=body,
                                 headers={"Content-Type": "application/json"})
    code, msg = None, ""
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
        code, msg = r.get("code"), r.get("msg")
        print("PUSH:", code, msg, "| data=", str(r.get("data"))[:120])
    except Exception as e:
        print("PUSH ERR:", str(e)[:80])
    # 邮件通道已按要求下线：日常只推微信（邮箱仅用于发《焚绝》）


def load_state():
    today = datetime.datetime.now(BJ).date().isoformat()
    month = today[:7]
    try:
        s = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        s = {}
    if s.get("date") != today:
        s["fired"] = []
        s["ref_cnt"] = 0        # 参考类信号当日计数（降噪用）
    if s.get("month") != month:
        s["count"] = 0
    s["date"] = today
    s["month"] = month
    s.setdefault("count", 0)
    s.setdefault("fired", [])
    return s


def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    json.dump(s, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False)


# ---------------- 报告生成 ----------------
def line_of(item, rt):
    code, name = item["code"], item["name"]
    r = rt.get(code)
    if not r:
        return "%s %s 行情获取失败" % (code, name)
    cost, sh = item.get("cost"), item.get("shares")
    parts = ["**%s %s** %.3f (%+.2f%%)" % (name, code, r["price"], r["chg"])]
    if cost:
        pnl = (r["price"] / cost - 1) * 100
        parts.append("成本%.3f 浮盈亏%+.1f%%" % (cost, pnl))
    return " ｜ ".join(parts)


def overnight_line():
    """隔夜外围：美股三大+黄金+原油（腾讯行情） + 隔夜要闻精选（新浪7x24）"""
    parts = []
    try:
        raw = http_get("https://qt.gtimg.cn/q=usDJI,usIXIC,usINX,hf_GC,hf_CL,hf_XAU", gbk=True)
        names = {"usDJI": "道指", "usIXIC": "纳指", "usINX": "标普", "hf_GC": "纽约金",
                 "hf_CL": "原油", "hf_XAU": "伦敦金"}
        for line in raw.strip().split(";"):
            if "=" not in line:
                continue
            k = line.split("=")[0].replace("v_", "").strip()
            v = line.split('="', 1)[1].rstrip('"') if '="' in line else ""
            if not v:
                continue
            if k.startswith("hf_"):
                p = v.split(",")
                if len(p) > 2:
                    parts.append("%s %s（%s%%）" % (names.get(k, k), p[0], p[1]))
            else:
                p2 = v.split("~")
                if len(p2) > 32:
                    parts.append("%s %s（%s%%）" % (names.get(k, k), p2[3], p2[32]))
    except Exception:
        pass
    lines = []
    if parts:
        lines.append("**隔夜外围**：" + " ｜ ".join(parts))
    try:
        j = json.loads(http_get("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=30&zhibo_id=152&tag_id=0&dire=f&dpc=1"))
        items = j.get("result", {}).get("data", {}).get("feed", {}).get("list", [])
        keys = ("美联储", "欧洲央行", "中国央行", "降息", "加息", "CPI", "PPI", "非农", "关税",
                "美股", "纳斯达克", "标普", "道琼斯", "英伟达", "台积电", "特斯拉", "A股", "沪深",
                "人民币", "证监会", "国常会")
        picked = []
        for it in items:
            txt = (it.get("rich_text") or it.get("text") or "").replace("\n", " ").strip()
            if any(w in txt for w in keys) and len(txt) > 10:
                t = (it.get("create_time") or "")[5:16]
                picked.append("%s %s" % (t, txt[:78]))
            if len(picked) >= 5:
                break
        if picked:
            lines.append("**隔夜要闻**：")
            lines += ["  · " + x for x in picked]
    except Exception:
        pass
    return "\n".join(lines)


def build_morning():
    """早盘盯防（9:25）：以昨收为基准，列出今天要盯的价位（近的、已触发的排前面）"""
    rt = realtime([i["code"] for i in POOL])
    today = datetime.datetime.now(BJ).date().isoformat()
    mk = market_line()
    lines = ["## 今日盯防（开盘前）%s" % today, ""] + ([mk, ""] if mk else [])
    ov = overnight_line()
    if ov:
        lines.append(ov)
        lines.append("")
    hot_blocks, normal_blocks = [], []
    for item in POOL:
        code, name = item["code"], item["name"]
        r = rt.get(code)
        if not r:
            continue
        _t = datetime.datetime.now(BJ).hour * 100 + datetime.datetime.now(BJ).minute
        base = r["price"] if _t < 915 else r["prev"]
        watches = []
        for lv in item.get("levels", []):
            p = lv["price"]
            try:
                dist = abs(p / base - 1)
            except Exception:
                continue
            fired = (lv["type"] == "down" and base < p) or (lv["type"] == "up" and base >= p)
            if dist <= 0.035 or fired:
                tag = "🔴" if (fired or dist <= 0.015) else "▫️"
                arrow = "跌破" if lv["type"] == "down" else "上破"
                watches.append("%s %.3f %s【%s】%s" % (
                    tag, p, lv["desc"], lv.get("ref", ""),
                    "（已达，等确认）" if fired else ""))
        if watches:
            blk = "- **%s** 昨收 %.3f ｜ 现 %+.2f%%\n" % (name, base, r["chg"]) + \
                  "\n".join("  · " + w for w in watches)
            if any("🔴" in w for w in watches):
                hot_blocks.append(blk)
            else:
                normal_blocks.append(blk)
    if hot_blocks or normal_blocks:
        lines += hot_blocks + normal_blocks
    else:
        lines.append("- 今日无临近价位，静观即可")
    lines.append("")
    # 今日荐股：体系款 + 打板款（各 1 只；已剔除涨停/ST/新股）
    lines.append("### 今日荐股")
    try:
        b1 = pick_stock()
        if b1:
            base1 = b1.get("price") or b1.get("close")
            log_pick("体系", b1["code"], b1["name"], base1, "评分%d" % b1["score"])
            lines.append("**【体系款】%s（%s）** 现 %.3f ｜ %s ｜ 评分 %d/100" % (
                b1["name"], b1["code"], base1, "；".join(b1["why"][:3]), b1["score"]))
            if b1.get("defense") and b1.get("ma10") and b1.get("target"):
                lines.append("- 关键位：止损/防线 %.3f ｜ 支撑 %.3f（MA10） ｜ 压力 %.3f" % (
                    b1["defense"], b1["ma10"], b1["target"]))
        else:
            lines.append("**【体系款】** 今日无符合标准的标的（宁缺毋滥）")
    except Exception as e:
        lines.append("**【体系款】** 扫描失败：%s" % str(e)[:60])
    try:
        b2 = pick_daban()
        if b2:
            log_pick("打板", b2["code"], b2["name"], b2["close"], "打板审美")
            lines.append("**【打板款】%s（%s）** 现 %.3f ｜ 涨 %.1f%% ｜ %s" % (
                b2["name"], b2["code"], b2["close"], b2["chg"], b2.get("vdisp") or ("量比%.2f" % b2["vr"])))
            lines.append("- 看点：%s（未封板可低吸；次日看竞价强弱，「弱转强」才是机会）" % b2["why"])
            if b2.get("note"):
                lines.append("- ⚠️ %s" % b2["note"])
        else:
            lines.append("**【打板款】** 今日无符合标准的标的（宁缺毋滥）")
    except Exception as e:
        lines.append("**【打板款】** 扫描失败：%s" % str(e)[:60])
    lines.append("")
    lines.append("> 开盘后触发买卖信号会自动推送；本清单每交易日 9:25 送达")
    return "\n".join(lines)


def open_mode(r):
    """开盘性质判断（2026-11-09 自建：解决"我这票涨4个点算不算分歧"）
    分歧 = 低开+缩量（有人卖、没人抢）→ 可小仓低吸
    一致 = 高开或已涨≥2%（抢筹、没有分歧）→ 追高区，等回踩
    转强 = 低开后拉回翻红 → 分歧转一致，回踩可跟
    """
    if not r.get("open") or not r.get("prev"):
        return "中性", ""
    gap = (r["open"] / r["prev"] - 1) * 100
    if abs(gap) > 12:
        return "中性", ""
    chg = r.get("chg") or 0.0
    vr = r.get("vr") or 1.0
    if gap <= -0.8 and vr < 1.2:
        return "分歧", "低开%.1f%%+缩量(量比%.2f) → 可小仓低吸" % (gap, vr)
    if gap >= 1.5 or chg >= 2.0:
        return "一致", "开%.1f%%/现%.1f%% → 追高区，不追；等回踩分时均线" % (gap, chg)
    if gap < -0.3 and chg > 0.3:
        return "转强", "低开%.1f%%后拉回%.2f%% → 分歧转一致，回踩可跟" % (gap, chg)
    return "中性", "开%.1f%%/现%.1f%% → 方向不明，等" % (gap, chg)


def build_intraday(tag):
    rt = realtime([i["code"] for i in POOL])
    mk = market_line()
    lines = ["## 策略池中盘对策（%s）" % tag, ""] + ([mk, ""] if mk else [])
    # ===== 开盘性质（9:25-10:00 才有意义，2026-11-09 加）=====
    try:
        _hm = datetime.datetime.now(BJ).strftime("%H%M")
        if "0925" <= _hm <= "1000":
            _srt = {}
            if SECTOR_ETF_REF:
                try:
                    _srt = realtime([v[0] for v in SECTOR_ETF_REF.values()])
                except Exception:
                    _srt = {}
            _rows = []
            for _it in POOL:
                if _it.get("skip_pos"):
                    continue
                _r = rt.get(_it["code"])
                if not _r:
                    continue
                _mode, _sug = open_mode(_r)
                _ref = SECTOR_ETF_REF.get(_it["code"])
                if _ref:
                    _sr = _srt.get(_ref[0]) or {}
                    if _sr.get("open") and _sr.get("prev"):
                        _sgap = (_sr["open"] / _sr["prev"] - 1) * 100
                        if _mode == "一致" and _sgap <= -0.8:
                            # 个股高开但板块低开 → 按板块判（b40：分歧层级看板块，不看个股）
                            _mode = "分歧"
                            _sug = "板块%s低开%.1f%%（本票高开）→ **按板块判=分歧，可小仓低吸**" % (
                                _ref[1], _sgap)
                        else:
                            _sug += "｜板块%s开%+.1f%%" % (_ref[1], _sgap)
                _icon = {"分歧": "🟢", "一致": "🔴", "转强": "🟡", "中性": "⚪"}.get(_mode, "")
                _rows.append("  %s **%s**：%s｜%s" % (_icon, _it["name"], _mode, _sug))
            if _rows:
                lines += ["**🎬 开盘性质（分歧=机会 / 一致=别追；板块与个股不一致时以板块为准）**"] + _rows + [""]
    except Exception:
        pass
    # 详细版：阶段 + 对策 + 量能台阶 + 今日重点（用户 2026-10-25 要求整点写详细）
    try:
        _rn, _rname, _rtip, _rpos, _rev = regime()
        lines += ["**📍 阶段 %s·%s**｜仓位上限 %d 成｜%s" % (_rn, _rname, _rpos, _rtip), ""]
        _adv = attack_advice()
        if _adv:
            lines += [_adv.split("\n")[-1], ""]
    except Exception:
        pass
    try:
        # 上证 5 日线：反弹的短期强弱线（2026-10-26 学自饕餮/邹老师盘面，现价贴 5 日线）
        _ks = kline("sh000001", 12)
        _sh = realtime(["sh000001"]).get("sh000001") or {}
        if len(_ks) >= 6 and _sh:
            _cl = [b[2] for b in _ks]
            _ma5 = sum(_cl[-5:]) / 5.0
            _pos = "上方" if _sh["price"] >= _ma5 else "下方"
            _gap = (_sh["price"] / _ma5 - 1) * 100
            lines += ["**📏 上证 5 日线 %.2f**｜现价 %.2f 在其**%s**（%+.2f%%）——跌破 5 日线 = 反弹走弱" % (
                _ma5, _sh["price"], _pos, _gap), ""]
            # 3600 关口（老刘 10-29：跌破 3600 才有资金愿意接；3620-3600 是下一档）
            try:
                if _sh:
                    _p = _sh["price"]
                    if _p < 3600:
                        lines += ["> 🎯 **已跌破 3600**（现价 %.2f）——老刘：跌破 3600 才有资金愿意接，**这里开始是机会区（但要等量能放大确认）**" % _p, ""]
                    elif _p < 3620:
                        lines += ["> 📍 **进入 3620-3600 支撑档**（现价 %.2f）——老刘：下一档看这里，3650 破了不算大事" % _p, ""]
            except Exception:
                pass
            # 3574 最后防线（小寒 10-29：不破 3574 就是筑底震荡，破了是另一个级别）
            try:
                if _sh and _sh["price"] < 3574:
                    lines += ["> 🚨🚨 **跌破 3574 最后防线**（现价 %.2f）——小寒：破了就不是筑底震荡，是**另一个级别**，**先减仓**" % _sh["price"], ""]
                elif _sh and _sh["price"] < 3600:
                    lines += ["> 🚨 **逼近 3574 最后防线**（现价 %.2f，差 %.0f 点）——小寒：不破就还是筑底震荡，**别恐慌；破了立刻减**" % (
                        _sh["price"], _sh["price"] - 3574), ""]
            except Exception:
                pass
            # 3650 支撑 / 3700 压力（老刘+小寒+教主 三家共识，2026-10-28）
            try:
                if _sh:
                    _px = _sh["price"]
                    if _px < 3650:
                        lines += ["> 🚨 **跌破 3650 支撑**（现价 %.2f）——三家共识的下沿破了：震荡起稳失效，**减仓**" % _px, ""]
                    elif _px <= 3700:
                        lines += ["> 📦 **在核心震荡区 3650-3700 内**（现价 %.2f）——上有 3700 压力、下有 3650 支撑；**箱体中间不追，等下沿低吸或上沿突破**" % _px, ""]
                    else:
                        lines += ["> ⬆️ **已突破 3700 压力**（现价 %.2f）——看向 3741（第一目标），站稳 3 天才算突破有效" % _px, ""]
            except Exception:
                pass
            # 机器人「5日线最多看两天」规则（小寒 10-28）
            try:
                _rb = realtime(["sz159559"]).get("sz159559") or {}
                _rbk = kline("sz159559", 8)
                if _rb and _rbk:
                    _rbma5 = sum(b[2] for b in _rbk[-5:]) / 5.0
                    _closes = [b[2] for b in _rbk[-2:]]
                    if _rb["price"] >= _rbma5:
                        lines += ["> ✅ **机器人站上 5 日线**（%.3f ≥ %.3f）——小寒规则：站稳就格局" % (_rb["price"], _rbma5), ""]
                    else:
                        _yest_below = _closes[0] < _rbma5 if len(_closes) > 1 else False
                        if _yest_below:
                            lines += ["> 🚨 **机器人连续 2 天在 5 日线下方**（%.3f < %.3f）——小寒规则：**最多看两天，今天该走**" % (_rb["price"], _rbma5), ""]
                        else:
                            lines += ["> ⚠️ **机器人跌破 5 日线**（%.3f < %.3f）——小寒规则：**给一天观察，明天还站不回就走**" % (_rb["price"], _rbma5), ""]
            except Exception:
                pass
            # 3741 关口：我们的「上方第一目标」（不是清仓线，2026-10-28 修正理解）
            # 饕餮说的"跌破3741三天不上就清仓"指的是 10-13 那波跌破支撑；对我们 3741 是上方缺口目标
            try:
                _k3 = kline("sh000001", 12)
                _up_days = 0
                for _b in reversed(_k3):
                    if _b[2] >= 3741:
                        _up_days += 1
                    else:
                        break
                if _up_days >= 3:
                    lines += ["> ✅ **已站上 3741 第 %d 天**（≥3 天）→ 反弹升级为二级修复，可加仓" % _up_days, ""]
                elif _up_days > 0:
                    lines += ["> ⏳ **3741 上方第 %d 天**（需站稳 3 天才算突破）" % _up_days, ""]
                else:
                    lines += ["> ⚠️ **3741 未站上**（上方缺口/第一目标）——**站不上 3741 就还是弱势震荡**，别把反抽当反转", ""]
            except Exception:
                pass
            # 科创50 新低提示（强龙 10-27：AI 硬件杀跌，科创50 是重灾区）            # 科创50 新低提示（强龙 10-27：AI 硬件杀跌，科创50 是重灾区）
            try:
                _kc = realtime(["sh588000"]).get("sh588000") or {}
                _kcks = kline("sh588000", 30)
                if _kc and _kcks:
                    _low20 = min(b[4] for b in _kcks[-20:])   # 近20日最低
                    if _kc["price"] <= _low20 * 1.005:
                        lines += ["> ⚠️ **科创50 逼近/创 20 日新低**（现价 %.3f，20日低 %.3f）——AI 硬件链条仍在杀跌，半导体类持仓要格外小心" % (
                            _kc["price"], _low20), ""]
            except Exception:
                pass
            # 3700 关口（教主 10-26：不上 3700 算不上反弹）
            if _sh and _sh["price"] < 3700:
                lines += ["> ⚠️ **3700 关口未上**（现价 %.2f，差 %.2f 点）——教主：连 3700 都上不了，算不上反弹，更别说反转" % (
                    _sh["price"], 3700 - _sh["price"]), ""]
            else:
                lines += ["> ✅ **站上 3700 关口**（现价 %.2f）——反弹成立的前提，下一个看 3741" % _sh["price"], ""]
            # 三季报密集期提醒（学自邹老师 10-26）
            _md5 = datetime.datetime.now(BJ).strftime("%m%d")
            if "1025" <= _md5 <= "1031":
                lines += ["> ⚠️ **三季报密集期（10-28 前后）+ 28 号解禁 + 周四美联储**：业绩没出的别加；踩雷的反弹到成本先减", ""]
    except Exception:
        pass
    try:
        _ratio, _amt, _full, _pg = vol_pace()
        if _amt:
            _need = vol_threshold((realtime(["sh000001"]).get("sh000001") or {}).get("price"))
            _flag = "达标（按推算全天）" if (_full or 0) >= _need else "不足（需 %.2f 万亿）" % _need
            _line = "**📊 量能 %.2f 万亿**（进度 %.0f%%）｜推算全天 **%.2f 万亿**｜该档门槛 %.2f → **%s**" % (
                _amt, _pg, _full or 0, _need, _flag)
            if _ratio:
                _line += "｜同期比 **%+.0f%%**" % ((_ratio - 1) * 100)
            lines += [_line, ""]
            # 老刘三档目标
            if _full:
                if _full >= 1.8:
                    lines += ["> 按老刘三档：**≥1.8 → 志存高远，可看 3800**", ""]
                elif _full >= 1.6:
                    lines += ["> 按老刘三档：**1.6-1.8 → 受压缺口，3741-3800 减 1/3**", ""]
                else:
                    lines += ["> 按老刘三档：**1.4-1.6 → 量能不足，只当反抽，别加仓**", ""]
                if _full < 1.4:
                    lines += ["> ⚠️ **量能已退回 1.4 万亿以下 = 昙花一现档**（老刘）：反弹结束，**清到 2 成以下**", ""]
    except Exception:
        pass
    layer_rows = []
    for item in POOL:
        code = item["code"]
        r = rt.get(code)
        sg = None
        if r:
            try:
                sg = signals(kline(code))
            except Exception:
                sg = None
        body = []
        hot = False
        if sg:
            trend = "空头排列" if sg["kong"] else "非空头"
            _vy = vs_yesterday(code, r)
            _vystr = ("｜ 较昨同期 **%+.0f%%**（%s）" % (_vy, "放量" if _vy >= 0 else "缩量")) if _vy is not None else ""
            _vpt2, _vph2 = vp_tag(r)
            _vpstr = ("｜ **%s**：%s" % (_vpt2, _vph2)) if _vpt2 else ""
            body.append("  · 趋势：%s（MA5 %.3f / MA20 %.3f / MA60 %.3f）｜ 量比 %.2f%s%s" % (
                trend, sg["m5"], sg["m20"], sg["m60"], r.get("vr") or 0, _vystr, _vpstr))
            if sg["hits"]:
                for no, desc in sg["hits"]:
                    t2 = "（空头排列中，只观察不执行）" if (no.startswith("B") and sg["kong"]) else ""
                    txt = "触发【%s %s】%s" % (no, desc, t2)
                    if t2:
                        body.append("  · %s" % txt)
                    else:
                        hot = True
                        body.append("  · 🔴 <font color=\"red\">**%s**</font>" % txt)
            else:
                body.append("  · 今日暂无 B/S 触发")
        broke_half = broke_full = level_touch = False
        if r:
            for lv in item.get("levels", []):
                st, _ = level_status(r["price"], lv)
                if st:
                    if st == "跌破":
                        if "全清" in lv.get("desc", ""):
                            broke_full = True
                        else:
                            broke_half = True
                    if st == "触及":
                        level_touch = True
                    txt = "【%s】%.3f %s（%s【%s】）" % (st, lv["price"], lv["desc"], lv.get("note", ""), lv.get("ref", ""))
                    if st in ("跌破", "触及"):
                        hot = True
                        body.append("  · 🔴 <font color=\"red\">**%s**</font>" % txt)
                    else:
                        body.append("  · %s" % txt)
        if sg and not item.get("skip_pos"):
            ly, why = position_layers(sg, sg["hits"], broke_half, broke_full, level_touch)
            if ly is not None:
                body.append("  · **目标仓位：%d/10 层**（%s）" % (ly, why))
                layer_rows.append((item["name"], ly))
        nl = line_of(item, rt)
        lines.append("- 🔴 <font color=\"red\">%s</font>" % nl if hot else "- " + nl)
        lines.extend(body)
        lines.append("")
    cap, tot = CFG.get("cap_per_stock"), CFG.get("total_capital")
    if cap and tot and layer_rows:
        yuan = sum(ly for _, ly in layer_rows) / 10.0 * cap
        lines.append("")
        lines.append("**建议总仓位：约 %.1f 成**（%s）" % (
            yuan / tot * 10, "、".join("%s %d/10" % (n, l) for n, l in layer_rows)))
    if cap and tot:
        lines.append("> 仓位口径：目标仓位=此刻该拿住的水平（10层=满配，满配=%.1f万/票）；动作行=今天做什么" % (cap / 10000.0))
    else:
        lines.append("> 仓位口径：目标仓位=此刻该拿住的水平；动作行=今天做什么")
    return "\n".join(lines)


def build_week():
    """周复盘（周五 16:30）：本周表现 + 教学点 + 下周预案（用户 2026-10-21 定）"""
    rt = realtime([i["code"] for i in POOL] + ["sh000001", "sz399006"])
    mk = market_line()
    today = datetime.datetime.now(BJ).date()
    monday = (today - datetime.timedelta(days=today.weekday())).isoformat()
    lines = ["## 本周复盘 + 下周预案（%s ~ %s）" % (monday[5:], today.isoformat()[5:]), ""]
    if mk:
        lines += [mk, ""]
    # 一、本周大盘
    lines += ["### 一、本周大盘", ""]
    for code, name in (("sh000001", "上证"), ("sz399006", "创业板")):
        try:
            ks = kline(code, 8)
            if len(ks) >= 6:
                c0, c1 = ks[-6][2], ks[-1][2]
                lines.append("- **%s** 周涨跌 **%+.2f%%**（%.2f → %.2f）" % (name, (c1 / c0 - 1) * 100, c0, c1))
        except Exception:
            pass
    # 二、策略池本周表现
    lines += ["", "### 二、策略池本周表现", ""]
    for item in POOL:
        try:
            ks = kline(item["code"], 8)
            r = rt.get(item["code"])
            if len(ks) >= 6 and r:
                w = (ks[-1][2] / ks[-6][2] - 1) * 100
                lines.append("- **%s** 周 **%+.2f%%** ｜ 现 %.3f（%+.2f%%）" % (item["name"], w, r["price"], r["chg"]))
        except Exception:
            pass
    # 三、本周信号与教学点
    lines += ["", "### 三、本周信号统计", ""]
    try:
        st = load_state()
        log = st.get("push_log") or []
        wlog = [p for p in log if p.get("ts", "")[:10] >= monday]
        lines.append("- 本周推送 **%d** 条（累计本月 %d 条）" % (len(wlog), st.get("count", 0)))
        fired = st.get("fired", [])
        lines.append("- 本周触发信号类别：%s" % ("、".join(sorted({f.split("|")[0] for f in fired})) or "无"))
    except Exception:
        pass
    # 四、死扛不动 vs 按体系做（用户 2026-10-21 要求：每周对比，检验体系有效性）
    lines += ["", "### 四、死扛不动 vs 按体系做（本周模拟）", ""]
    _bh_sum, _sys_sum, _rows = 0.0, 0.0, []
    for _item in POOL:
        try:
            _ks = kline(_item["code"], 15)
            _seg = [b for b in _ks if b[0] >= monday]
            if len(_seg) < 2:
                continue
            _buy = _seg[0][2]
            _bh = (_seg[-1][2] / _buy - 1) * 100
            _pos, _eq, _prev, _notes = 1.0, 1.0, _buy, []
            for _b in _seg:
                if _prev:
                    _eq *= (1 + _pos * (_b[2] / _prev - 1))
                _prev = _b[2]
                for _lv in _item.get("levels", []):
                    _p, _t = float(_lv["price"]), _lv["type"]
                    _dsc = _lv.get("desc", "")
                    if _t == "down" and _b[2] < _p and _pos > 0:
                        _np = 0.0 if "全清" in _dsc else 0.5
                        if _np < _pos:
                            _notes.append("%s破%.3f%s" % (_b[0][5:], _p, "清" if _np == 0 else "减半"))
                            _pos = _np
                    elif _t == "up" and _b[2] >= _p and _pos < 1.0:
                        _notes.append("%s站上%.3f回满" % (_b[0][5:], _p))
                        _pos = 1.0
            _sy = (_eq - 1) * 100
            _bh_sum += _bh
            _sys_sum += _sy
            _rows.append("- **%s**：死扛 %+.2f%% ｜ 体系 %+.2f%%%s" % (
                _item["name"], _bh, _sy, ("（" + "；".join(_notes) + "）") if _notes else "（本周无信号）"))
        except Exception:
            pass
    if _rows:
        _n = max(1, len(_rows))
        lines += _rows
        _diff = (_sys_sum - _bh_sum) / _n
        lines += ["", "- **合计（等权）**：死扛 **%+.2f%%** ｜ 体系 **%+.2f%%** ｜ **体系%s %.2f 个百分点**" % (
            _bh_sum / _n, _sys_sum / _n, "多赚" if _diff >= 0 else "少亏", abs(_diff))]
    else:
        lines.append("- 本周无完整日线数据，略")

    # 策略池量能同比 + 板块对比（用户 2026-10-23 要求）
    try:
        _rows = pool_volume_pace()
        if _rows:
            lines += ["", "### 五、策略池量能（较昨日同期）", ""]
            for _n, _a, _pct, _est in _rows:
                _flag = "🔺" if (_pct or 0) >= 30 else ("🔻" if (_pct or 0) <= -30 else "➖")
                lines.append("- **%s** 今日 **%.2f 亿** ｜ 较昨日同期 **%+.0f%%** %s ｜ 预计全天 **%.2f 亿**"
                             % (_n, _a, (_pct or 0), _flag, _est or 0))
            _tot = sum(r[1] for r in _rows)
            lines.append("- **合计 %.2f 亿**（池内 7 只）" % _tot)
    except Exception:
        pass
    try:
        _pv = pool_vs_peer()
        if _pv:
            lines += ["", "### 六、策略池 vs 板块（跟随还是独立）", ""]
            for _n, _c, _pn, _pc, _v, _sec in _pv:
                lines.append("- **%s** %+.2f%% ｜ %s（%s）%+.2f%% → %s" % (_n, _c, _pn, _sec, _pc, _v))
            lines.append("> 读法：跟随板块=板块行情；**独立走强=有独立逻辑（可能是主线）**；独立走弱=自身有问题")
    except Exception:
        pass

    lines += ["", "### 五、教学点（本周最该记住的 3 条）"]
    lines += ["1. **量能是唯一的确认**：1.4-1.5 万亿只能磨，1.7 万亿以上才有像样反弹（b26 三口径）",
              "2. **破位后的第一反抽通常不是底**：反抽≠反弹（b21）——先看能否收回 3724/3741",
              "3. **扣底=均线走平**：明日站上 N 天前收盘，均线才走平（4.5）——先算价再等收盘"]
    # 四、下周预案
    lines += ["", "### 六、下周预案", ""]
    lines += ["- **第一防线 3674**（924 起点）：破了看 3650，再下 3600 分水岭",
              "- **上沿 3741**：收回 3724=假跌破第一条件，站上 3741=假跌破成立可分批接",
              "- **量能**：<1.2 万亿=只减不加；≥1.5 万亿=可做；≥1.7 万亿=放心做",
              "- **纪律**：手上票三季报没出的别碰；破位先减不猜底（s6）"]
    content = "\n".join(lines)
    return content


def build_close():
    rt = realtime([i["code"] for i in POOL])
    today = datetime.datetime.now(BJ).date().isoformat()
    mk = market_line()
    try:
        _rn, _rname, _rtip, _rpos, _rev = regime()
        _rline = "**📍 当前市场阶段：%s·%s**（仓位上限 %d 成）｜%s" % (_rn, _rname, _rpos, _rtip)
        _rline2 = "> %s" % _rev
    except Exception:
        _rline, _rline2 = "", ""
    lines = (["## 收盘复盘 + 明日预案（%s）" % today, "", _rline, _rline2, ""] if _rline else
             ["## 收盘复盘 + 明日预案（%s）" % today, ""]) + ([mk, ""] if mk else []) + ["### 今日状态", ""]
    tomorrow = []
    # 三季报密集期提醒（学自邹老师 10-26：反弹有先后，别踩雷）
    try:
        _md = datetime.datetime.now(BJ).strftime("%m%d")
        if "1025" <= _md <= "1031":
            tomorrow.append("**⚠️ 三季报密集披露期（10-28 前后）+ 28 号解禁 + 周四美联储**：手上票业绩没出的不要加仓；踩雷的票反弹到成本区先减")
    except Exception:
        pass
    layer_rows = []
    for item in POOL:
        code = item["code"]
        r = rt.get(code)
        sg = None
        if r:
            try:
                sg = signals(kline(code))
            except Exception:
                sg = None
        hot = False
        body = []
        if sg:
            trend = "空头排列" if sg["kong"] else "非空头"
            body.append("  · 趋势：%s" % trend)
            if sg["hits"]:
                hot_txt = "收盘触发 " + "、".join("【%s】%s" % (n, d) for n, d in sg["hits"])
                real_hot = any(not (n.startswith("B") and sg["kong"]) for n, d in sg["hits"])
                if real_hot:
                    hot = True
                    body.append("  · 🔴 <font color=\"red\">**%s**</font>" % hot_txt)
                else:
                    body.append("  · %s" % hot_txt)
            else:
                body.append("  · 无 B/S 触发")
        broke_half = broke_full = level_touch = False
        if r:
            for lv in item.get("levels", []):
                st, _ = level_status(r["price"], lv)
                if st == "跌破":
                    if "全清" in lv.get("desc", ""):
                        broke_full = True
                    else:
                        broke_half = True
                if st == "触及":
                    level_touch = True
        if sg and not item.get("skip_pos"):
            ly, why = position_layers(sg, sg["hits"], broke_half, broke_full, level_touch)
            if ly is not None:
                body.append("  · **目标仓位：%d/10 层**（%s）" % (ly, why))
                layer_rows.append((item["name"], ly))
        nl = line_of(item, rt)
        lines.append("- 🔴 <font color=\"red\">%s</font>" % nl if hot else "- " + nl)
        lines.extend(body)
        lines.append("")
        for lv in item.get("levels", []):
            arrow = "上破" if lv["type"] == "up" else "跌破"
            tomorrow.append("- **%s**：%s %.3f → %s【%s】" % (item["name"], arrow, lv["price"], lv["desc"], lv.get("ref", "")))
    cap, tot = CFG.get("cap_per_stock"), CFG.get("total_capital")
    if cap and tot and layer_rows:
        yuan = sum(ly for _, ly in layer_rows) / 10.0 * cap
        lines.append("")
        lines.append("**建议总仓位：约 %.1f 成**（%s）" % (
            yuan / tot * 10, "、".join("%s %d/10" % (n, l) for n, l in layer_rows)))
    # 策略池量能同比 + 板块对比（用户 2026-10-23 要求）
    try:
        _rows = pool_volume_pace()
        if _rows:
            lines += ["", "### 策略池量能（较昨日同期）", ""]
            for _n, _a, _pct, _est in _rows:
                _flag = "🔺" if (_pct or 0) >= 30 else ("🔻" if (_pct or 0) <= -30 else "➖")
                lines.append("- **%s** 今日 **%.2f 亿** ｜ 较昨日同期 **%+.0f%%** %s ｜ 预计全天 **%.2f 亿**"
                             % (_n, _a, (_pct or 0), _flag, _est or 0))
            lines.append("- **池内合计 %.2f 亿**" % sum(r[1] for r in _rows))
    except Exception:
        pass
    try:
        _pv = pool_vs_peer()
        if _pv:
            lines += ["", "### 策略池 vs 板块 vs 龙头（三层对比）", ""]
            for _n, _c, _sec, _sc, _ln, _lc, _v in _pv:
                lines.append("- **%s** %+.2f%% ｜ 板块「%s」%+.2f%%（3 只代表股均值）｜ 龙头 %s %+.2f%%\n  → %s"
                             % (_n, _c, _sec, _sc, _ln, (_lc if _lc is not None else 0), _v))
            lines.append("> 读法：**板块内最强**=强于板块且不弱于龙头（最值得做）；"
                         "强于板块但龙头更强=跟风；**弱于板块**=拖后腿（该换）")
    except Exception:
        pass

    lines.append("### 明日盯防（挂条件单/预警）")
    lines.extend(tomorrow)
    lines.append("")
    lines.append(track_picks())
    lines.append("")
    if cap and tot:
        lines.append("> 仓位口径：目标仓位=此刻该拿住的水平（10层=满配，满配=%.1f万/票）；动作行=今天做什么" % (cap / 10000.0))
    lines.append("> 盘中触发买/卖信号会自动推送告警")
    return "\n".join(lines)


# ---------------- 今日荐股（仅 close 模式调用） ----------------
def _snapshot():
    """全市场快照：东财（多域名轮询，全市场约5000只）为主，全挂自动降级新浪涨幅榜（前400）"""
    for host in ("push2.eastmoney.com", "82.push2.eastmoney.com", "48.push2.eastmoney.com"):
        try:
            rows = []
            for pn in range(1, 13):
                url = ("https://%s/api/qt/clist/get?pn=%d&pz=500&po=1&np=1&fltt=2&invt=2"
                       "&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
                       "&fields=f12,f14,f2,f3,f10,f20,f21,f24,f26" % (host, pn))
                d = json.loads(http_get(url))
                diff = (d.get("data") or {}).get("diff") or []
                if not diff:
                    break
                rows += diff
            if len(rows) > 800:
                return rows
        except Exception:
            continue
    rows = []
    for page in range(1, 6):
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "Market_Center.getHQNodeData?page=%d&num=80&sort=changepercent&asc=0&node=hs_a" % page)
        try:
            arr = json.loads(http_get(url))
        except Exception:
            break
        if not isinstance(arr, list) or not arr:
            break
        for it in arr:
            code = str(it.get("code", ""))
            if not code or len(code) != 6:
                continue
            try:
                chg = float(it.get("changepercent"))
            except Exception:
                continue
            rows.append({"f12": code, "f14": str(it.get("name", "")), "f3": chg,
                         "f2": float(it.get("trade", 0) or 0), "f10": None, "f20": None,
                         "f24": None, "f26": ""})
    return rows


def _fallback_watch(rows):
    """兜底观察票：快照里挑最有动能的一只（涨 2.5-9.5%、尽量非高位），补拉 K 线拿价格"""
    best = None
    for it in rows:
        name = str(it.get("f14", ""))
        code = str(it.get("f12", ""))
        if not code or "ST" in name or "退" in name:
            continue
        try:
            chg = float(it.get("f3"))
        except Exception:
            continue
        if not (2.5 <= chg <= 9.5):
            continue
        dd60 = it.get("f24")
        if isinstance(dd60, (int, float)) and dd60 > -5:
            continue
        sc = chg + (min(6.0, -dd60 / 8.0) if isinstance(dd60, (int, float)) else 0)
        if not best or sc > best["_sc"]:
            best = {"code": ("sh" if code.startswith(("6", "5")) else "sz") + code,
                    "name": name, "chg": chg, "level": "观察", "_sc": sc,
                    "score": int(40 + sc), "typ": "动能观察", "vr": 0, "close": None,
                    "why": ["全市场动能扫描（暂未出体系买点，列入观察）"]}
    if best and best.get("close") is None:
        try:
            bars = kline(best["code"], 140)
            sg = signals(bars)
            if sg:
                best["close"] = sg["close"]
                best["stop"] = bars[-1][4]
                best["target"] = max(b[3] for b in bars[-120:])
        except Exception:
            pass
    return best


_PICK_META = {}


def pick_stock():
    """全市场扫描 -> 主推（B类+非空头）或备选（空头B类/逼近型）；没有就 None（宁缺毋滥）"""
    try:
        rows = _snapshot()
    except Exception:
        rows = []
    _PICK_META["snapshot"] = len(rows)
    if not rows:
        return None
    now = datetime.datetime.now(BJ)
    cand = []
    for it in rows:
        name = str(it.get("f14", ""))
        code = str(it.get("f12", ""))
        if not code or "ST" in name or "退" in name:
            continue
        listed = str(it.get("f26", "") or "")
        if listed.isdigit() and len(listed) == 8:
            try:
                if (now - datetime.datetime.strptime(listed, "%Y%m%d")).days < 120:
                    continue
            except Exception:
                pass
        try:
            chg = float(it.get("f3"))
        except Exception:
            continue
        if chg >= (19.7 if code.startswith(("30", "68")) else 9.7):
            continue  # 涨停/接近涨停：买不进且无空间，不推荐
        vr = it.get("f10")
        dd60 = it.get("f24")
        try:
            vrf = float(vr) if vr is not None else None
        except Exception:
            vrf = None
        try:
            f2 = float(it.get("f2")) if it.get("f2") else None
        except Exception:
            f2 = None
        if chg >= 5 and (vrf is None or vrf >= 1.8):
            cand.append(("强势突破", code, name, chg, f2))
        elif vrf is not None and isinstance(dd60, (int, float)) and dd60 <= -30 and vrf <= 0.8 and chg > 0:
            cand.append(("超跌反转", code, name, chg, f2))
    strong = [c for c in cand if c[0] == "强势突破"][:40]
    low = [c for c in cand if c[0] == "超跌反转"][:40]
    best1 = best2 = None
    for typ, code, name, chg, f2 in strong + low:
        tcode = ("sh" if code.startswith(("6", "5")) else "sz") + code
        try:
            bars = kline(tcode, 140)
            sg = signals(bars)
        except Exception:
            continue
        if not sg:
            continue
        hits = sg["hits"]
        real = [(n, d) for n, d in hits if not (n.startswith("B") and sg["kong"])]
        v = [b[5] for b in bars]
        vr_k = 0
        try:
            vr_k = v[-1] / (sum(v[-6:-1]) / 5.0)
        except Exception:
            pass
        hh = max(b[3] for b in bars[-120:])
        dd = (hh - sg["close"]) / hh
        hh20_prev = max(b[3] for b in bars[-21:-1])
        ma10 = sum(b[2] for b in bars[-10:]) / 10.0
        defense = hh20_prev if sg["close"] >= hh20_prev * 0.99 else float(bars[-1][4])

        def _score(real_hits, kong, vr_k, dd):
            s = max({"B1": 35, "B2": 40, "B3": 30, "B4": 35}.get(n, 20) for n, d in real_hits) if real_hits else 0
            why = ["、".join("【%s %s】" % (n, d) for n, d in real_hits)] if real_hits else []
            if sg["m5"] > sg["m20"] > sg["m60"]:
                s += 25; why.append("多头排列")
            elif not kong:
                s += 12; why.append("非空头")
            else:
                s += 4; why.append("空头(仅观察)")
            if 1.8 <= vr_k <= 3:
                s += 20; why.append("量比%.2f 黄金区间" % vr_k)
            elif 3 < vr_k <= 5:
                s += 12; why.append("量比%.2f" % vr_k)
            elif vr_k >= 1.2:
                s += 8; why.append("量比%.2f" % vr_k)
            elif vr_k > 0:
                why.append("量比%.2f" % vr_k)
            if 0.15 <= dd <= 0.45:
                s += 15; why.append("位置健康(距高点-%.0f%%)" % (dd * 100))
            elif dd > 0.45:
                s += 10; why.append("深跌位(距高点-%.0f%%)" % (dd * 100))
            else:
                s += 4; why.append("高位(距高点-%.0f%%)" % (dd * 100))
            return min(s, 100), why

        item = {"code": tcode, "name": name, "close": sg["close"], "stop": bars[-1][4],
                "target": hh, "defense": defense, "ma10": ma10,
                "price": float(f2 or sg["close"]),
                "typ": typ, "chg": chg, "vr": round(vr_k, 2)}
        if real:
            sc, why = _score(real, sg["kong"], vr_k, dd)
            if not sg["kong"] and (not best1 or sc > best1["score"]):
                best1 = dict(item, score=sc, why=why, level="主推")
        lvl2 = (hits and sg["kong"]) or (chg >= 4 and vr_k >= 1.5 and dd <= 0.06 and not sg["kong"])
        if lvl2:
            sc, why = _score(real, sg["kong"], vr_k, dd)
            sc = int(sc * 0.8) if real else min(sc, 62)
            if not best2 or sc > best2["score"]:
                best2 = dict(item, score=sc, why=why, level="备选")
    pick = best1 or best2
    if pick and pick.get("score", 0) < 80:
        pick = None   # 80 分门槛（2026-10-16 用户要求：低于 80 不推，宁缺毋滥）
    return pick


def _free_mv(code):
    """流通市值（亿元）：腾讯行情第 45 位为主，失败返回 None"""
    try:
        txt = http_get("https://qt.gtimg.cn/q=%s" % code, gbk=True)
        parts = txt.split("~")
        for idx in (44, 45):
            if len(parts) > idx:
                try:
                    v = float(parts[idx])
                    if 1 <= v <= 20000:
                        return v
                except Exception:
                    pass
    except Exception:
        pass
    return None


def _daban_audit(code, price):
    """按《打板手册（融合版）》审美核验：连板数 / 距前高 / 放量
    返回 dict：lim=连板数, hi=距60日新高%, vol=今日量/5日均量"""
    try:
        bars = kline(code, 70)
    except Exception:
        return {}
    if not bars or len(bars) < 15:
        return {}
    closes = [b[2] for b in bars]      # [date,open,close,high,low,vol]
    vols = [b[5] for b in bars]
    # 连板数：从最后一根往前数"涨幅≥9.8%"
    lim = 0
    for k in range(len(closes) - 1, 0, -1):
        if closes[k - 1] > 0 and (closes[k] / closes[k - 1] - 1) >= 0.098:
            lim += 1
        else:
            break
    # 距60日新高（不含最后3日，避免自身影响）
    seg = closes[-63:-3] if len(closes) > 63 else closes[:-3]
    hi60 = max(seg) if seg else price
    hi = (price / hi60 - 1) * 100 if hi60 else 0
    v5 = sum(vols[-6:-1]) / 5.0 if len(vols) >= 6 else vols[-1]
    volr = (vols[-1] / v5) if v5 else 1.0
    return {"lim": lim, "hi": hi, "volr": volr, "hi60": hi60}


def pick_daban():
    """【打板款·2026-10-19 起按《打板手册（融合版）》审美】
    硬指标：主板10cm / 可流通市值 20~60 亿 / 低价（≤12元）/ 未涨停（3%~9.7%）/ 量比≥1.5
    加分项：2~3板内（≤3）/ 距 60 日新高 ≤10%（"两三个板就创新高"）/ 放量（≥1.5 倍5日均量）
    明确回避：20cm（创业/科创）、北交所、ST/退/N、上市<120天
    返回 dict 或 None"""
    try:
        rows = _snapshot()
    except Exception:
        rows = []
    if not rows:
        return None
    now = datetime.datetime.now(BJ)
    cands = []
    for it in rows:
        name = str(it.get("f14", ""))
        code = str(it.get("f12", ""))
        if not code or "ST" in name or "退" in name or name.startswith("N"):
            continue
        if not code.startswith(("60", "00")):
            continue                      # 手册 3.5：回避 20cm 与北交所
        listed = str(it.get("f26", "") or "")
        if listed.isdigit() and len(listed) == 8:
            try:
                if (now - datetime.datetime.strptime(listed, "%Y%m%d")).days < 120:
                    continue
            except Exception:
                pass
        try:
            chg = float(it.get("f3"))
            vr = float(it.get("f10") or 0)
            price = float(it.get("f2") or 0)
        except Exception:
            continue
        if price <= 0 or price > 12:
            continue                      # 手册 3.2②："没有哪个龙头是超过十块钱起步的"
        if chg >= 9.7 or chg < 3:
            continue                      # 未涨停（买得进）+ 有强度
        # 可流通市值 20~60 亿（手册 2.1：20~30 亿最舒服，放宽到 60）
        fmv = it.get("f21")
        tmv = it.get("f20")
        if not isinstance(fmv, (int, float)) or fmv <= 0:
            fmv = (tmv * 0.6) if isinstance(tmv, (int, float)) and tmv > 0 else None
        if fmv and not (20e8 <= fmv <= 60e8):
            continue
        cands.append((it, code, name, price, chg, vr, fmv))
    # 按"强度×量能×低价"粗排，取前 8 只再拉K线核验（控请求量）
    cands.sort(key=lambda x: -(x[4] * 10 + min(x[5], 4) * 5 + max(0, 12 - x[3])))
    for it, code, name, price, chg, vr, fmv in cands[:8]:
        full = ("sh" if code.startswith("6") else "sz") + code

        # ① K线核验（连板数 / 距新高 / 量能倍数）——拿不到就不推
        a = _daban_audit(full, price)
        if not a:
            continue
        if a["volr"] < 1.2:
            continue                     # 手册 b34：量不到，一切反弹只是反抽

        # ② 流通市值复核（腾讯行情）——手册 2.1：20~30 亿最舒服，放宽到 60
        fmv_yi = _free_mv(full)
        if fmv_yi is not None:
            if not (20 <= fmv_yi <= 60):
                continue
        elif fmv:
            fmv_yi = fmv / 1e8
        else:
            continue                     # 两头都拿不到流通市值 → 不猜，跳过

        score = chg * 10 + min(vr, 4) * 5 + max(0, 12 - price)
        why = ["主板10cm", "价%.2f" % price, "涨%.1f%%未涨停" % chg,
               ("量比%.2f" % vr) if vr else ("量能%.1f倍(5日均)" % a["volr"]),
               "流通%.0f亿" % fmv_yi]

        # ③ 加分与排除（手册第二章 / 3.2）
        if a["lim"] >= 1:
            if a["lim"] > 3:
                continue                 # 3 板以上=跟风区，不做
            why.append("%d连板" % a["lim"])
            score += 6 * a["lim"]
        if a["hi"] >= -10:
            why.append("距60日新高%.1f%%" % a["hi"])
            score += 8                   # "两三个板就创新高"=上方无套牢
        elif a["hi"] < -30:
            score -= 10                  # 头顶一堆套牢盘
        if a["volr"] >= 1.5:
            why.append("放量确认")
            score += 5

        vdisp = ("量比%.2f" % vr) if vr else ("量能%.1f倍" % a["volr"])
        item = {"code": full, "name": name, "chg": chg, "vr": round(vr, 2), "close": price,
                "vdisp": vdisp,
                "score": int(min(99, score)), "why": "；".join(why),
                "note": "打板审美：需 F10 核「5%以上股东占比≥30%」再动手"}
        if score >= 80:
            return item
    return None


# ---- 荐股账本：盘前荐 2 只，收盘跟踪 5 个交易日 ----
def _picks_path():
    return os.path.join(os.path.dirname(STATE_FILE), "picks.json")


def load_picks():
    try:
        return json.load(open(_picks_path(), encoding="utf-8"))
    except Exception:
        return {"active": [], "done": []}


def save_picks(pf):
    try:
        os.makedirs(os.path.dirname(_picks_path()), exist_ok=True)
        json.dump(pf, open(_picks_path(), "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass


def log_pick(kind, code, name, base, note):
    """盘前荐股入账（同日同代码去重）"""
    pf = load_picks()
    today = datetime.datetime.now(BJ).date().isoformat()
    for p in pf["active"]:
        if p["date"] == today and p["code"] == code:
            return
    pf["active"].append({"date": today, "kind": kind, "code": code, "name": name,
                         "base": base, "note": note, "log": []})
    save_picks(pf)
    print("PICK-LOG: %s %s(%s) base=%.3f %s" % (kind, name, code, base, note), flush=True)


def track_picks():
    """收盘跟踪：更新近5日荐股表现并输出；满5个交易日归档"""
    pf = load_picks()
    today = datetime.datetime.now(BJ).date().isoformat()
    if not pf["active"]:
        return "**⭐ 荐股跟踪**：暂无在跟标的（每交易日 9:25 荐 2 只：体系款+打板款）"
    rt = realtime([p["code"] for p in pf["active"]])
    lines = ["**⭐ 荐股跟踪（近5个交易日）**"]
    still = []
    for p in pf["active"]:
        r = rt.get(p["code"])
        c = r["price"] if r else None
        if c and (not p["log"] or p["log"][-1][0] != today):
            p["log"].append([today, c, round((c / p["base"] - 1) * 100, 2)])
        cur = p["log"][-1][2] if p["log"] else 0.0
        day = None
        if len(p["log"]) >= 2:
            day = round((p["log"][-1][1] / p["log"][-2][1] - 1) * 100, 2)
        n = len(p["log"])
        seg = "今 %+.2f%% ｜ " % day if day is not None else ""
        lines.append("- 【%s】%s（%s 荐）｜ %s累计 %+.2f%% ｜ 第 %d/5 天" % (
            p["kind"], p["name"], p["date"][5:], seg, cur, min(n, 5)))
        if n >= 5:
            pf["done"].append(p)
        else:
            still.append(p)
    pf["active"] = still
    save_picks(pf)
    # 月度胜率（用户 2026-10-21 要求）：按"体系款/打板款"分别统计满 5 日标的
    try:
        month = today[:7]
        stat = {}
        for p in (pf.get("done", []) + still):
            if len(p.get("log", [])) >= 5 and p.get("date", "")[:7] == month:
                k = p.get("kind", "?")
                w, t = stat.get(k, (0, 0))
                stat[k] = (w + (1 if p["log"][4][2] > 0 else 0), t + 1)
        if stat:
            segs = []
            for k, (w, t) in stat.items():
                segs.append("%s %d/%d（%.0f%%）" % (k, w, t, 100.0 * w / t))
            lines.append("- **📈 本月胜率**：" + " ｜ ".join(segs))
    except Exception:
        pass
    return "\n".join(lines)


def pick_line():
    b = pick_stock()
    if not b:
        n = _PICK_META.get("snapshot", 0)
        if n == 0:
            return ("**⭐ 今日荐股**：⚠️ 数据源异常（全市场扫描未完成）——今日无推荐，"
                    "属系统问题、非行情问题")
        return ("**⭐ 今日荐股**：全市场扫描完成（%d 只候选），**暂无符合体系的标的**——"
                "空仓等信号也是操作（✓ 数据链路正常）" % n)
    lv = b.get("level", "主推")
    if lv == "主推":
        head = "**⭐ 今日荐股：%s（%s）｜ 评分 %d/100**" % (b["name"], b["code"], b["score"])
        tail = "- 仅供次日参考，系统不跟踪"
    elif lv == "备选":
        head = "**⭐ 今日看点【备选】：%s（%s）｜ 评分 %d/100**" % (b["name"], b["code"], b["score"])
        tail = ("- 说明：今日市场偏弱，暂无满分标的——此为相对最优，尚未完全触发买点，只做关注\n"
                "- 仅供次日参考，系统不跟踪")
    else:
        head = "**⭐ 今日看点【观察】：%s（%s）｜ 动能分 %d**" % (b["name"], b["code"], b["score"])
        tail = ("- 说明：暂未出体系买点，先放观察池——等放量突破/底分型出现再动手\n"
                "- 仅供次日参考，系统不跟踪")
    lines = [head, "- 类型：%s ｜ 今日 %+.1f%%" % (b["typ"], b["chg"])]
    if b.get("vr"):
        lines[1] += "，量比 %.2f" % b["vr"]
    lines.append("- 理由：%s" % "；".join(b["why"]))
    if b.get("defense") and b.get("ma10") and b.get("target"):
        lines.append("- 关键位：")
        lines.append("  · 止损/防线 **%.3f**（前20日高/突破位）——跌回下方 = 突破失败，走" % b["defense"])
        lines.append("  · 支撑 **%.3f**（MA10）——回踩不破可轻仓试" % b["ma10"])
        lines.append("  · 压力 **%.3f**（120日高=前高套牢区）——到附近减半" % b["target"])
    elif b.get("close") and b.get("stop") and b.get("target"):
        lines.append("- 预测：守住 %.3f 看震荡上行，上方压力 %.3f；跌破 %.3f = 逻辑破，离场" % (
            b["stop"], b["target"], b["stop"]))
    lines.append(tail)
    return "\n".join(lines)


def build_monitor():
    """盘中监控：仅在有新触发时返回内容，否则 None"""
    state = load_state()
    rt = realtime([i["code"] for i in POOL])
    alerts = []
    _MODE = market_mode()   # 市场状态（strong/weak/normal）——决定个股信号放宽还是收紧
    # 大盘关键位（config 的 market 段；2026-10-16 验证日布置）
    try:
        mkrt = realtime(["sh000001"]).get("sh000001")
        if mkrt:
            for lv in CFG.get("market", {}).get("levels", []):
                key = "market|level|%s|%s" % (lv["type"], lv["price"])
                st, fired = level_status(mkrt["price"], lv)
                if fired and key not in state["fired"]:
                    state["fired"].append(key)
                    alerts.append(("🔴 <font color=\"red\">**【上证指数】现价 %.2f %s %.2f**</font>\n**动作：%s**（【%s】）" % (
                        mkrt["price"], "上破" if lv["type"] == "up" else "跌破",
                        lv["price"], lv["desc"], lv.get("ref", "")), key, "market|level|" + lv["type"]))
            save_state(state)
    except Exception:
        pass
    # 银行 5 日线信号（小寒"右侧信号"；与 Q8 框架互证）
    try:
        bkbars = kline("sz399986", 10)
        bkrt = realtime(["sz399986"]).get("sz399986")
        if bkrt and len(bkbars) >= 5:
            ma5 = sum(b[2] for b in bkbars[-5:]) / 5.0
            key = "bank|ma5|break"
            if bkrt["price"] < ma5 * 0.995:
                if key not in state["fired"]:
                    state["fired"].append(key)
                    if _ref_ok(state):
                        alerts.append(("🔵 【中证银行】现价 %.2f **跌破 5 日线**（MA5 %.2f）\n**小寒右侧信号出现**：资金或开始从防御切换——关注科技/证券承接（Q8 框架）" % (bkrt["price"], ma5), key, "bank|ma5"))
            else:
                if key in state["fired"]:
                    state["fired"].remove(key)
            save_state(state)
    except Exception:
        pass
    # 券商风向标（强龙：等券商，定进退）——券商 ETF 涨跌幅绝对值 ≥1.5% 或站上/跌破 5 日线
    try:
        _qb = realtime(["sh512000"]).get("sh512000")
        if _qb:
            _kb = kline("sh512000", 10)
            _ma5 = sum(b[2] for b in _kb[-5:]) / 5.0 if len(_kb) >= 5 else None
            _k1 = "broker|move"
            _k2 = "broker|ma5|up"
            _k3 = "broker|ma5|down"
            if _ma5:
                if _qb["price"] >= _ma5 and _k2 not in state["fired"]:
                    state["fired"].append(_k2)
                    if _ref_ok(state):
                        alerts.append(("🟡 【券商ETF】%.3f **站上 5 日线**（MA5 %.3f，%+.2f%%）\n**强龙口径**：券商动了=右侧信号（「等券商，定进退」）→ 看能否放量确认" % (_qb["price"], _ma5, _qb["chg"]), _k2, "broker|ma5"))
                    if _k3 in state["fired"]:
                        state["fired"].remove(_k3)
                elif _qb["price"] < _ma5 and _k3 not in state["fired"]:
                    state["fired"].append(_k3)
                    if _ref_ok(state):
                        alerts.append(("🔵 【券商ETF】%.3f **跌破 5 日线**（MA5 %.3f，%+.2f%%）\n风向标转弱：右侧信号消失，注意题材退潮" % (_qb["price"], _ma5, _qb["chg"]), _k3, "broker|ma5"))
                    if _k2 in state["fired"]:
                        state["fired"].remove(_k2)
            if abs(_qb["chg"]) >= 1.5 and _k1 not in state["fired"]:
                state["fired"].append(_k1)
                if _ref_ok(state):
                    alerts.append(("🟡 【券商ETF】异动 **%+.2f%%**（现 %.3f）\n**强龙：券商是风向标**——先看是不是它带头，再决定追不追" % (_qb["chg"], _qb["price"]), _k1, "broker|move"))
            save_state(state)
    except Exception:
        pass
    # 创业板/宁德先行（小寒：宁德带崩创业板）
    try:
        _qm = realtime(["sz399006", "sz300750"])
        _cy = _qm.get("sz399006")
        _nd = _qm.get("sz300750")
        if _cy and _cy["chg"] <= -1.5:
            _k = "cyb|drop"
            if _k not in state["fired"]:
                state["fired"].append(_k)
                if _ref_ok(state):
                    alerts.append(("🔵 【创业板指】**%+.2f%%**（现 %.2f）\n小寒口径：**宁德带崩创业板**——先看宁王，再看题材；创业板破位，小票别硬扛" % (_cy["chg"], _cy["price"]), _k, "cyb|drop"))
        if _nd:
            _kn = kline("sz300750", 10)
            _nma = sum(b[2] for b in _kn[-5:]) / 5.0 if len(_kn) >= 5 else None
            if _nma and _nd["price"] < _nma:
                _k = "ningde|ma5|down"
                if _k not in state["fired"]:
                    state["fired"].append(_k)
                    if _ref_ok(state):
                        alerts.append(("🔵 【宁德时代】%.2f **跌破 5 日线**（MA5 %.2f，%+.2f%%）\n创业板先行指标转弱（小寒：「宁德带崩创业板」）" % (_nd["price"], _nma, _nd["chg"]), _k, "ningde|ma5"))
            elif _nma and "ningde|ma5|down" in state["fired"]:
                state["fired"].remove("ningde|ma5|down")
        save_state(state)
    except Exception:
        pass
    # 全市场同步异动（用户 10-23：13:00 开盘所有板块一起拉，这种特殊要播报）
    try:
        _sg, _n = market_surge()
        if _sg and "surge|all" not in state["fired"]:
            state["fired"].append("surge|all")
            alerts.append(("🚨 【全市场同步异动】%d 个宽基同时拉升\n%s\n"
                "**含义**：不是单板块护盘，是资金集体进场（主动性行情）——这种异动优先级高于个股信号，"
                "先看宽基能否收住，再决定跟不跟个股" % (_n, _sg), "surge|all", "surge|all"))
            save_state(state)
    except Exception:
        pass
    # 龙头异动（宁王/华天/中信等：龙头先动，板块才动）
    try:
        _lm = leader_move()
        for _name, _dir, _tag, _t, _ratio, _amt, _c5 in _lm:
            _k3 = "leader|%s|%s" % (_name, _t)
            if _k3 in state["fired"]:
                continue
            state["fired"].append(_k3)
            alerts.append(("🐲 【龙头异动】**%s**（%s）%s\n%s **%s**：该分钟成交 **%.1f 亿**（均额 %.1f 倍），5 分钟 **%+.2f%%**\n"
                "> 龙头先动、板块才动——看它的方向能不能带动同板块"
                % (_name, _tag, _t, _dir, _t, _amt, _ratio, _c5), _k3, "leader"))
        save_state(state)
    except Exception:
        pass
    # 分钟级脉冲放量（最高优先级：一两分钟内的快速放量）
    try:
        _hits = volume_pulse()
        if _hits:
            _last = _hits[-1]
            _k2 = "pulse|" + _last[0]
            if _k2 not in state["fired"]:
                state["fired"].append(_k2)
                _segs = "；".join("%s %s倍/%.1f亿" % (h[0], h[1], h[2]) for h in _hits[-3:])
                alerts.append(("⚡ 【分钟级脉冲放量】%s\n最近一笔：**%s 的 %.0f 亿**（前 20 分钟均量 **%.1f 倍**），"
                    "当时价 %.2f（%+.2f%%）\n"
                    "> **这是最重要的信号**：一两分钟内突然放量 = 有资金在扫货/砸盘。"
                    "配合方向看——拉升中的脉冲=进攻，下跌中的脉冲=出货或恐慌" % (_segs, _last[0], _last[2], _last[1], _last[3], _last[4]),
                    _k2, "pulse"))
                save_state(state)
    except Exception:
        pass
    # 量能节奏（5 个检查点：10:30 / 11:00 / 13:30 / 14:00 / 14:30）
    try:
        _hmz = datetime.datetime.now(BJ).strftime("%H:%M")
        if _hmz in ("10:30", "11:00", "13:30", "14:00", "14:30"):
            _ratio, _amt, _full, _pg = vol_pace()
            if _ratio is not None:
                _k = "volpace|" + _hmz
                if _k not in state["fired"]:
                    if _ratio >= 1.3:
                        state["fired"].append(_k)
                        alerts.append(("📊 【量能明显放大】当前 **%.2f 万亿**（进度 %.0f%%），"
                            "比过去 5 日同期 **高 %.0f%%**，推算全天约 **%.2f 万亿**\n"
                            "> 放量=资金在进，配合阶段判断：若是阶段 2→3，这就是反扑的燃料"
                            % (_amt, _pg, (_ratio - 1) * 100, _full), _k, "volpace"))
                    elif _ratio <= 0.7:
                        state["fired"].append(_k)
                        alerts.append(("📉 【量能明显萎缩】当前 **%.2f 万亿**（进度 %.0f%%），"
                            "比过去 5 日同期 **低 %.0f%%**，推算全天约 **%.2f 万亿**\n"
                            "> 缩量=没人接，别指望大涨；缩量下的反弹都是反抽"
                            % (_amt, _pg, (1 - _ratio) * 100, _full), _k, "volpace"))
                    save_state(state)
    except Exception:
        pass
    # 市场阶段迁移播报（状态机）
    try:
        _rn, _rname, _rtip, _rpos, _rev = regime()
        _prev = state.get("regime")
        if _rn and _prev and _rn != _prev and "regime|move" not in state["fired"]:
            state["fired"].append("regime|move")
            _SWITCH = {
                (1, 3): "**防守 → 抄底**", (2, 3): "**防守 → 抄底**",
                (3, 4): "**抄底 → 持有**", (4, 5): "**持有 → 趋势加仓**",
                (5, 6): "**持有 → 分批止盈**", (6, 7): "**止盈 → 清仓**",
                (7, 1): "**清仓 → 空仓防守**", (3, 1): "**抄底失败 → 退回防守**",
                (4, 1): "**修复失败 → 退回防守**",
            }
            _sw = _SWITCH.get((_prev, _rn), "阶段切换")
            alerts.append(("🔄 【市场阶段迁移 %s → %s·%s】\n**操作性质：%s**｜仓位上限 %d 成\n%s\n"
                "> %s" % (_prev, _rn, _rname, _sw, _rpos, _rtip, _rev), "regime|move", "regime"))
        if _rn:
            state["regime"] = _rn
        save_state(state)
    except Exception:
        pass
    # 止盈 / 别参与（用户 10-23：好行情要提醒止盈，末期要提醒别追）
    try:
        for _lvl, _msg in profit_signal():
            _k = "pt|" + _lvl
            if _k not in state["fired"]:
                state["fired"].append(_k)
                alerts.append((_msg, _k, "pt"))
        save_state(state)
    except Exception:
        pass
    # 缺口三定律（周老师 10-23：第1个逃、第2个等、第3个加）
    try:
        _gn, _gtip = gap_rule()
        if _gn and _gn >= 2:
            _gk = "gap|n|%d" % _gn
            if _gk not in state["fired"]:
                state["fired"].append(_gk)
                _glist = gap_count()[-4:]
                _gseg = "、".join("%s(%.0f点,%s)" % (d[5:], p, t) for d, p, t in _glist)
                alerts.append(("🕳 【向下跳空第 %d 个】%s\n按**缺口三定律**（周老师）：%s\n"
                    "- 依据：第 1 个是破位（逃）、第 2 个是加速（等）、**第 3 个是衰竭（加）**"
                    % (_gn, _gseg, _gtip), _gk, "gap"))
                save_state(state)
    except Exception:
        pass
    # 国家队入场（用户 10-23：宽基 ETF 突然放量 = 国家队在买，等它入场我们再入场）
    try:
        _lvl, _nt, _rows = national_team()
        _k = "nt|" + str(_lvl)
        if _lvl and _k not in state["fired"]:
            state["fired"].append(_k)
            _head = ("🚨 【宽基ETF集体放量 → **疑似国家队入场**】" if _lvl == "confirm"
                     else "🟡 【宽基ETF明显放量 → 资金异动预警】")
            _body = ("**这是博反弹的入场信号（b30）**：国家队筑底买的就是这些宽基，ETF 放量=真金白银。\n"
                     "- 动作：试仓 1/3-1/2，**不必等均线多头**（暴跌后本来就是空头）\n"
                     "- 目标：反抽 5 日线/前高；**跌破当日低点立刻走**" if _lvl == "confirm"
                     else "宽基放量但还没到极端（2.5 倍）——**候选信号，盯住**：若尾盘继续放大或明日再放，就升级为国家队入场。\n"
                          "- 现在可以做的是：把想博反弹的标的选好，等确认（b30）")
            alerts.append((_head + "\n" + _nt + "\n" + _body, _k, "nt"))
            save_state(state)
    except Exception:
        pass
    # 情绪维度（2026-10-23 新增，用户要求：冰点提示打最后一笔仓位）
    try:
        _se = sentiment()
        _zt, _dt = _se.get("zt"), _se.get("dt")
        if _zt is not None and _dt is not None:
            if (_dt >= 40 or _zt <= 20) and "sent|ice" not in state["fired"]:
                state["fired"].append("sent|ice")
                alerts.append(("🧊 【情绪冰点】涨停 **%d** 家 ／ 跌停 **%d** 家\n"
                    "**冰点=底部特征，可以打最后一笔仓位**（用户定义）：\n"
                    "- 做法：把计划仓位的**最后 2-3 成**分两批——今天先打一半，**明天不破今日低点再补另一半**\n"
                    "- 前提：指数当日必须**放量**（成交额较昨日放大），缩量冰点只能看不能买" % (_zt, _dt), "sent|ice", "sent|ice"))
            if (_zt >= 100 or _se.get("max_lb", 0) >= 7) and "sent|hot" not in state["fired"]:
                state["fired"].append("sent|hot")
                alerts.append(("🔥 【情绪过热】涨停 **%d** 家 ／ 最高 **%d** 连板\n"
                    "过热=减仓信号：别在这里加仓，涨得最猛的先减 1/3（s2）" % (_zt, _se.get("max_lb", 0)), "sent|hot", "sent|hot"))
            # 板块效应：涨停集中度 ≥5 家 → 主线提示（用户要求"某个板块突然放量也要提示"）
            _sn, _sc = _se.get("top_sector_n", 0), _se.get("top_sector")
            if _sc and _sn >= 5:
                _k = "sent|sector|" + str(_sc)
                if _k not in state["fired"]:
                    state["fired"].append(_k)
                    alerts.append(("🎯 【板块效应】**%s** 涨停 **%d** 家（全市场涨停 %d 家）\n"
                        "这是资金主攻方向——按「穿越性」标准验证它是不是主线（抗跌/独立/弹性/资金占比）" % (_sc, _sn, _zt), _k, "sent|sector"))
            save_state(state)
    except Exception:
        pass
    # 小盘/游资信号（强龙：微盘股创新高+高标=游资在场；微盘无公开指数，用中证1000代理）
    try:
        _kb = kline("sh000852", 30)
        _qz = realtime(["sh000852"]).get("sh000852")
        if _qz and len(_kb) >= 21:
            _hi = max(b[3] for b in _kb[-21:-1])
            _lo = min(b[4] for b in _kb[-21:-1])
            if _qz["price"] > _hi:
                _k = "small|newhigh"
                if _k not in state["fired"]:
                    state["fired"].append(_k)
                    if _ref_ok(state):
                        alerts.append(("🟡 【中证1000】%.2f **创 20 日新高**（前高 %.2f，%+.2f%%）\n**强龙口径**：小盘创新高=游资在做（机构没动）→ 题材活跃期，可跟强势股；但别追高" % (_qz["price"], _hi, _qz["chg"]), _k, "small|newhigh"))
            elif _qz["price"] < _lo:
                _k = "small|newlow"
                if _k not in state["fired"]:
                    state["fired"].append(_k)
                    if _ref_ok(state):
                        alerts.append(("🔵 【中证1000】%.2f **创 20 日新低**（前低 %.2f，%+.2f%%）\n游资退潮/小盘杀跌 → 题材退潮期，仓位往权重靠" % (_qz["price"], _lo, _qz["chg"]), _k, "small|newlow"))
            save_state(state)
    except Exception:
        pass
    # 量能分档（b26 多口径）：11:00 / 14:00 检查两市成交额，跨档提示一次
    try:
        _hm = datetime.datetime.now(BJ).strftime("%H:%M")
        if _hm in ("11:00", "14:00"):
            _raw = http_get("https://qt.gtimg.cn/q=sh000001,sz399001", gbk=True)
            _amt = 0.0
            for _line in _raw.strip().split(";"):
                if '="' not in _line:
                    continue
                _p = _line.split('="')[1].strip('"').split("~")
                if len(_p) > 37:
                    try:
                        _amt += float(_p[37]) * 10000  # 万元 → 元
                    except Exception:
                        pass
            if _amt > 0:
                _yi = _amt / 100000000.0
                try:
                    _shp = realtime(["sh000001"]).get("sh000001")
                    _need = vol_threshold(_shp["price"]) if _shp else 1.7
                except Exception:
                    _need = 1.7
                _tier = None
                _okv, _gdv, _cfv, _src, _same = vol_ref(_need and _shp["price"] if _shp else None)
                if _yi >= _cfv * 10000:
                    _tier = ("vol|tier|17", "🔥 量能 **%.2f 万亿** ≥ 确认级 %.1f 万亿（%s）——底部确认量" % (_yi / 10000, _cfv, _src))
                elif _yi >= _gdv * 10000:
                    _tier = ("vol|tier|18", "⚡ 量能 **%.2f 万亿** ≥ 达标 %.1f 万亿（%s）——可反攻" % (_yi / 10000, _gdv, _src))
                elif _yi >= _okv * 10000:
                    _tier = ("vol|tier|17", "🔥 量能 **%.2f 万亿** ≥ 及格 %.1f 万亿（%s）——能做" % (_yi / 10000, _okv, _src))
                elif _yi >= 15000:
                    _tier = ("vol|tier|15", "⚡ 量能 **%.2f 万亿** ≥ 1.5 万亿（邹老师门槛）——够做，但没到 1.7" % (_yi / 10000))
                elif _hm == "11:00" and _yi * 2 >= 17000:
                    _tier = ("vol|tier|pace", "🚀 半日量 **%.2f 万亿**，推算全天约 **%.2f 万亿**（≥1.7 万亿门槛）——放量确认，可动手（老刘：长下影阳线+量能达标=底部条件）" % (_yi / 10000, _yi * 2 / 10000))
                elif _hm == "14:00" and _yi * 1.15 >= 17000:
                    _tier = ("vol|tier|pace", "🚀 14:00 量已 **%.2f 万亿**，推算全天约 **%.2f 万亿**（≥1.7 万亿）——放量确认" % (_yi / 10000, _yi * 1.15 / 10000))
                elif _hm == "11:00" and _yi < 12000:
                    _tier = ("vol|tier|low", "⚠️ 半日量能仅 **%.2f 万亿**——量能不足，今天大概率还是「磨」（b26）" % (_yi / 10000))
                if _tier and _tier[0] not in state["fired"]:
                    state["fired"].append(_tier[0])
                    alerts.append(("📊 " + _tier[1], _tier[0], "vol|tier"))
                    save_state(state)
    except Exception:
        pass
    for item in POOL:
        code, name = item["code"], item["name"]
        r = rt.get(code)
        if not r:
            continue
        # 1) 关键位
        for lv in item.get("levels", []):
            key = "%s|level|%s|%s" % (code, lv["type"], lv["price"])
            st, fired = level_status(r["price"], lv)
            if fired and key not in state["fired"]:
                state["fired"].append(key)
                alerts.append(("🔴 <font color=\"red\">**【%s】现价 %.3f %s关键位 %.3f**</font>\n**动作：%s**（%s【%s】）" % (
                    name, r["price"], "跌破" if lv["type"] == "down" else "上破",
                    lv["price"], lv["desc"], lv.get("note", ""), lv.get("ref", "")),
                    key, "%s|level|%s" % (code, lv["type"])))
        # 2) 日线信号（盘中动态K线）
        try:
            sg = signals(kline(code))
        except Exception:
            sg = None
        if sg and sg["hits"]:
            for no, desc in sg["hits"]:
                key = "%s|sig|%s|%s" % (code, no, sg["date"])
                if key in state["fired"]:
                    continue
                state["fired"].append(key)
                act = "买入信号" if no.startswith("B") else "卖出信号"
                if no.startswith("B"):
                    _mm, _mdet = _MODE[0], _MODE[1]
                    if _mm == "rebound":
                        act += ("（**情绪反扑模式：空头排列不做限制**——连续大跌后本来就是空头，等排列就错过。"
                                "但这是**反弹交易不是趋势交易**：试仓 1/3，目标反抽 5 日线/前高，"
                                "**跌破今日低点立刻走，不恋战**）")
                    elif _mm == "strong":
                        act += "（趋势级强势：可执行，试仓 1/3，跌破今日低点止损）" if sg["kong"] else "（趋势级强势，可执行）"
                    elif _mm == "weak":
                        act += "（大盘弱势：只观察，不执行）"
                    elif sg["kong"]:
                        _rs, _rsd = reverse_strong(code)
                        if _rs:
                            act += ("（**反趋势特例：%s** —— 指数空头但它逆势走强，属主线级别的例外，"
                                    "**不受空头排列限制**：试仓 1/3-1/2，收盘破 5 日线即走）" % _rsd)
                        else:
                            act += "（空头排列，只观察；若出现情绪冰点+放量反扑，按反弹交易试仓 1/3）"
                _vpt, _vph = vp_tag(r)
                _vy = vs_yesterday(code, r)
                _vytxt = ""
                if _vy is not None:
                    _vytxt = "｜较昨同期 **%+.0f%%**（%s）" % (_vy, "放量" if _vy >= 0 else "缩量")
                if _vpt:
                    _vpline = "\n> 量价：**%s**（量比 %.2f）%s——%s" % (_vpt, r.get("vr", 0), _vytxt, _vph)
                elif _vytxt:
                    _vpline = "\n> 量能：%s" % _vytxt.lstrip("｜")
                else:
                    _vpline = ""
                alerts.append(("🔴 <font color=\"red\">**【%s】触发%s【%s %s】**</font>\n现价 %.3f（%+.2f%%）%s" % (
                    name, act, no, desc, r["price"], r["chg"], _vpline), key, "%s|sig" % code))
    save_state(state)
    if not alerts:
        print("无新触发")
        return None
    # 通知分级：P2 参考级单次最多 3 条、当日累计最多 6 条（用户 2026-10-23 要求优化通知）
    try:
        _p2 = [a for a in alerts if alert_level(a[2]) == "P2"]
        _main = [a for a in alerts if alert_level(a[2]) != "P2"]
        _p2_used = state.get("p2_cnt", 0)
        _p2_keep = max(0, min(3, 6 - _p2_used))
        if _p2:
            _main += _p2[:_p2_keep]
            if len(_p2) > _p2_keep:
                _main.append(("…另有 %d 条参考信号（%s）未展开，避免刷屏" % (
                    len(_p2) - _p2_keep, "、".join(a[2].split("|")[0] for a in _p2[_p2_keep:])), "p2cut|" + str(len(_p2)), "P2CUT"))
            state["p2_cnt"] = _p2_used + len(_p2[:_p2_keep])
        alerts = _main
    except Exception:
        pass
    # 每条预警开头都带上"大盘状态 + 攻防建议"（用户 2026-10-23 要求）
    try:
        _adv = attack_advice()
        if _adv:
            alerts.insert(0, (_adv, "adv|" + str(datetime.datetime.now(BJ).strftime("%H%M")), "adv"))
    except Exception:
        pass
    # 重点摘要（用户 2026-10-25：把最重要的信息突出出来）
    try:
        _PRI = ("pulse", "leader", "regime", "nt", "sent", "gap", "pt", "volpace", "surge")
        def _prio(a):
            for _i, _p in enumerate(_PRI):
                if (a[2] or "").startswith(_p):
                    return _i
            return 99
        _core = [a for a in alerts if a[2] != "adv" and _prio(a) < 99]
        if _core:
            _best = sorted(_core, key=_prio)[0]
            _first_line = [l for l in _best[0].splitlines() if l.strip()][0]
            _first_line = _first_line.replace('<font color="red">', '').replace('</font>', '').replace('**', '')
            alerts.insert(1, ("**⚡ 重点：%s**" % _first_line[:60], "top|" + str(id(_best)), "top"))
    except Exception:
        pass
    # 去重：按「涨幅变化幅度」判定（用户 2026-10-25 定）
    # 规则：同一标的，只有当涨幅比上次推送时变化 ≥2 个点（或方向反转）才再报；
    #      变化 <2 点视为"同一意思"，静默。这样既灵敏又不重复。
    import re as _re
    _last = state.setdefault("last_chg", {})
    _lastts = state.setdefault("last_ts", {})
    _now = datetime.datetime.now(BJ)
    final = []
    for _a in alerts:
        _text, _key, _sk = _a
        _m = _re.search(r"^(sh|sz)\d{6}", _sk or "")
        _code = _m.group(0) if _m else None
        _chm = _re.search(r"([+-]?\d+\.?\d*)%", _text)
        _chg = float(_chm.group(1)) if _chm else None
        _ok = True
        if _code and _chg is not None and _code in _last:
            _prev = _last[_code]
            _delta = abs(_chg - _prev)
            _flip = (_prev > 0 > _chg) or (_prev < 0 < _chg)
            _last_t = _lastts.get(_code)
            _gap_min = 99
            if _last_t:
                try:
                    _dt = datetime.datetime.fromisoformat(_last_t)
                    if _dt.tzinfo is None:
                        _dt = _dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
                    _gap_min = (_now - _dt).total_seconds() / 60.0
                except Exception:
                    pass
            _vpchg = "量价：" in _text and ("背离" in _text or "派" in _text or "别接" in _text)
            if _delta < 2.0 and not _flip and not _vpchg and _gap_min < 30:
                _ok = False           # 涨幅变化不够大且无量价背离 → 静默
        if _ok:
            if _code and _chg is not None:
                _last[_code] = _chg
                _lastts[_code] = _now.isoformat()
            final.append(_text)
    save_state(state)
    if not final:
        print("去重：涨幅变化不足 2 个点，静默")
        return None
    t = datetime.datetime.now(BJ).strftime("%H:%M")
    return "## 触发告警 %s\n\n" % t + "\n\n---\n\n".join(final)


# ---------------- 入口 ----------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "intraday"
    # P1 防限流：随机抖动 0~20s，避开与其它任务的整点碰撞
    time.sleep(random.uniform(0, 20))
    now = datetime.datetime.now(BJ).strftime("%m-%d %H:%M")
    try:
        if not is_trading_day():
            print("[%s] 今日休市（行情时间戳不是今天），静默退出" % now)
            sys.exit(0)
        if not in_window(mode):
            print("[%s] 非有效时段，静默退出防误报（mode=%s）" % (now, mode))
            sys.exit(0)
        if mode == "morning":
            push("【今日盯防】" + now, build_morning())
        elif mode == "intraday":
            hm = datetime.datetime.now(BJ).strftime("%H:%M")
            st_i = load_state()
            du = st_i.get("defer_until")
            if du:
                try:
                    ddt = datetime.datetime.fromisoformat(du)
                    if ddt.tzinfo is None:
                        ddt = ddt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
                    if datetime.datetime.now(BJ) < ddt and ddt.date() == datetime.datetime.now(BJ).date():
                        print("[%s] 本班对策已顺延至 %s（此前有提前告警），静默" % (hm, ddt.strftime("%H:%M")))
                        sys.exit(0)
                except Exception:
                    pass
            push("【策略池对策】" + now, build_intraday(now))
        elif mode == "close":
            if datetime.datetime.now(BJ).weekday() == 4:      # 周五 → 周复盘
                _c = build_week()
                push("【本周复盘+下周预案】" + now, _c)
                save_archive("周复盘", _c)
            else:
                _c = build_close()
                push("【收盘复盘+明日预案】" + now, _c)
                save_archive("日复盘", _c)
        elif mode == "monitor":
            hm = datetime.datetime.now(BJ).strftime("%H:%M")
            if "09:25" <= hm <= "09:36":
                print("[%s] 早盘盯防窗口（09:25-09:36）告警静默，避免与盯防连发" % hm)
                sys.exit(0)
            st_m = load_state()
            du = st_m.get("defer_until")
            # 自愈：顺延时间已过 / 不是今天 → 清掉，避免残留吞告警
            if du:
                try:
                    _dd = datetime.datetime.fromisoformat(du)
                    if _dd.tzinfo is None:
                        _dd = _dd.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
                    if _dd <= datetime.datetime.now(BJ) or _dd.date() != datetime.datetime.now(BJ).date():
                        st_m.pop("defer_until", None)
                        st_m.pop("deferred_pushed", None)
                        save_state(st_m)
                        du = None
                except Exception:
                    st_m.pop("defer_until", None)
                    save_state(st_m)
                    du = None
            if du:
                try:
                    ddt = datetime.datetime.fromisoformat(du)
                    if ddt.tzinfo is None:
                        ddt = ddt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
                    if ddt.date() == datetime.datetime.now(BJ).date():
                        if datetime.datetime.now(BJ) < ddt:
                            # 2026-10-26 用户明确：只防重复，不是不让推！
                            # 顺延期只影响"对策补推"，**告警照常走去重逻辑（按幅度）**
                            print("[%s] 对策顺延至 %s（告警不受影响，仍按幅度去重后推送）" % (hm, ddt.strftime("%H:%M")))
                        if not st_m.get("deferred_pushed"):
                            push("【策略池对策·顺延】" + now, build_intraday("顺延 %s" % ddt.strftime("%H:%M")))
                            st_m["deferred_pushed"] = True
                            save_state(st_m)
                            print("顺延对策已推")
                            sys.exit(0)
                except Exception:
                    pass
            content = build_monitor()
            if content:
                la = st_m.get("last_push_ts") or st_m.get("last_alert_ts")
                if la:
                    try:
                        _la = datetime.datetime.fromisoformat(la)
                        if _la.tzinfo is None:
                            _la = _la.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
                        _gap = (datetime.datetime.now(BJ) - _la).total_seconds()
                        # 2026-10-26 用户明确：只防重复，不拦新信息。
                        # 20 分钟冷却会吞掉真实异动 → 缩到 3 分钟（只防同一波重复推）
                        if _gap < 180:
                            print("[%s] 距上次播报 %d 秒（<3 分钟），同一波静默" % (hm, int(_gap)))
                            sys.exit(0)
                    except Exception:
                        pass
                push("【触发告警】" + now, content)
                st_m = load_state()
                st_m["last_alert_ts"] = datetime.datetime.now(BJ).isoformat()
                _now = datetime.datetime.now(BJ)
                # 2026-10-26 用户要求：整点对策（10:30/14:00/14:50）**一律必推，不再顺延**
                # 原"提前告警 → 顺延对策"规则取消（曾导致 10:30 对策被吞）
                save_state(st_m)
        else:
            print("未知模式:", mode)
    except Exception:
        import traceback
        err = traceback.format_exc()
        print(err)
        push("【系统异常】" + now, "监控脚本出错：\n\n```\n" + err[-800:] + "\n```")
        raise
