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
            }
        except Exception:
            continue
    return out


def kline(code, n=140):
    """腾讯前复权日线 -> [[date,open,close,high,low,vol], ...]（盘中最后一根为动态）"""
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq"
           % (code, n))
    j = json.loads(http_get(url))
    d = j["data"][code]
    bars = d.get("qfqday") or d.get("day")
    return [[b[0], float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])] for b in bars]


def is_trading_day():
    """A股交易日自检：上证指数实时行情的最后成交时间是否今天（自动识别节假日，零维护）"""
    try:
        raw = http_get("https://qt.gtimg.cn/q=sh000001", gbk=True)
        p = raw.split('="')[1].split("~")
        ts = p[30] if len(p) > 30 else ""
        return ts.startswith(datetime.datetime.now(BJ).strftime("%Y%m%d"))
    except Exception:
        return True


def in_window(mode, t=None):
    """有效时段守卫（北京时间）：防止 GitHub 延迟触发的班次在盘后乱推（误导性信号）。
    intraday/monitor: 9:25-11:35 或 12:55-15:05；close: 15:00-23:59；其余一律静默。"""
    if t is None:
        _n = datetime.datetime.now(BJ)
        t = _n.hour * 100 + _n.minute
    if mode in ("intraday", "monitor"):
        return (925 <= t <= 1135) or (1255 <= t <= 1505)
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
    prev_b5 = sty > 0 and syy / sty >= 0.5 and l[i - 1] <= m20p * 1.02 and c[i - 1] >= c[i - 2] * 0.97

    hits = []
    if zf >= 0.05 and vr >= 1.8 and c[i - 1] <= m20p < c[i]:
        hits.append(("B1", "标志性大阳线（放量+5%站上20日线）"))
    if c[i] > hh20p and vr >= 1.8:
        hits.append(("B2", "放量真突破（破20日高+量比1.8）"))
    if c[i] > hh20p and c[i - 1] > max(h[i - 21:i - 1]):
        hits.append(("B3", "两天确认（连续站上20日高）"))
    if prev_b5 and c[i] >= c[i - 1]:
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
def push(title, content):
    if not TOKEN:
        print("[WARN] 未配置 PUSHPLUS_TOKEN，跳过推送")
        print(content)
        return
    st = load_state()
    st["count"] = st.get("count", 0) + 1
    save_state(st)
    content += "\n\n---\n\n📊 本月第 %d 次推送 ｜ PushPlus 免费额度充足" % st["count"]
    body = json.dumps({"token": TOKEN, "title": title, "content": content,
                       "template": "markdown"}).encode("utf-8")
    req = urllib.request.Request("https://www.pushplus.plus/send", data=body,
                                 headers={"Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
    print("PUSH:", r.get("code"), r.get("msg"))


def load_state():
    today = datetime.datetime.now(BJ).date().isoformat()
    month = today[:7]
    try:
        s = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        s = {}
    if s.get("date") != today:
        s["fired"] = []
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


def build_intraday(tag):
    rt = realtime([i["code"] for i in POOL])
    mk = market_line()
    lines = ["## 策略池中盘对策（%s）" % tag, ""] + ([mk, ""] if mk else [])
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
            body.append("  · 趋势：%s（MA5 %.3f / MA20 %.3f / MA60 %.3f）｜ 量比 %.2f" % (
                trend, sg["m5"], sg["m20"], sg["m60"], sg.get("vr", 0)))
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


def build_close():
    rt = realtime([i["code"] for i in POOL])
    today = datetime.datetime.now(BJ).date().isoformat()
    mk = market_line()
    lines = ["## 收盘复盘 + 明日预案（%s）" % today, ""] + ([mk, ""] if mk else []) + ["### 今日状态", ""]
    tomorrow = []
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
    lines.append("### 明日盯防（挂条件单/预警）")
    lines.extend(tomorrow)
    lines.append("")
    lines.append(pick_line())
    lines.append("")
    if cap and tot:
        lines.append("> 仓位口径：目标仓位=此刻该拿住的水平（10层=满配，满配=%.1f万/票）；动作行=今天做什么" % (cap / 10000.0))
    lines.append("> 盘中触发买/卖信号会自动推送告警")
    return "\n".join(lines)


# ---------------- 今日荐股（仅 close 模式调用） ----------------
def _snapshot():
    """全市场快照：东财为主，自动降级新浪涨幅榜（统一结构 f12/f14/f3/f10/f24/f26）"""
    try:
        rows = []
        for pn in range(1, 13):
            url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=500&po=1&np=1&fltt=2&invt=2"
                   "&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
                   "&fields=f12,f14,f2,f3,f10,f24,f26" % pn)
            d = json.loads(http_get(url))
            diff = (d.get("data") or {}).get("diff") or []
            if not diff:
                break
            rows += diff
        if len(rows) > 800:
            return rows
    except Exception:
        pass
    rows = []
    for page in (1, 2):
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "Market_Center.getHQNodeData?page=%d&num=80&sort=changepercent&asc=0&node=hs_a" % page)
        try:
            arr = json.loads(http_get(url))
        except Exception:
            break
        if not isinstance(arr, list):
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
                         "f10": None, "f24": None, "f26": ""})
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


def pick_stock():
    """全市场扫描 -> 分级返回：主推（B类+非空头）/ 备选（空头B类或逼近型）/ 观察（兜底）"""
    try:
        rows = _snapshot()
    except Exception:
        rows = []
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
        vr = it.get("f10")
        dd60 = it.get("f24")
        try:
            vrf = float(vr) if vr is not None else None
        except Exception:
            vrf = None
        if chg >= 5 and (vrf is None or vrf >= 1.8):
            cand.append(("强势突破", code, name, chg))
        elif vrf is not None and isinstance(dd60, (int, float)) and dd60 <= -30 and vrf <= 0.8 and chg > 0:
            cand.append(("超跌反转", code, name, chg))
    strong = [c for c in cand if c[0] == "强势突破"][:40]
    low = [c for c in cand if c[0] == "超跌反转"][:40]
    best1 = best2 = None
    for typ, code, name, chg in strong + low:
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
                "target": hh, "typ": typ, "chg": chg, "vr": round(vr_k, 2)}
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
    if best1:
        return best1
    if best2:
        return best2
    return _fallback_watch(rows)


def pick_line():
    b = pick_stock()
    if not b:
        return "**⭐ 今日荐股**：全市场扫描失败（数据源异常），明天再看"
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
    if b.get("close") and b.get("stop") and b.get("target"):
        lines.append("- 预测：守住 %.3f 看震荡上行，上方压力 %.3f；跌破 %.3f = 逻辑破，离场" % (
            b["stop"], b["target"], b["stop"]))
        lines.append("- 应对：回踩 %.3f 不破可轻仓试；止损 %.3f；到 %.3f 附近减半" % (
            b["close"] * 0.985, b["stop"], b["target"]))
    lines.append(tail)
    return "\n".join(lines)


def build_monitor():
    """盘中监控：仅在有新触发时返回内容，否则 None"""
    state = load_state()
    rt = realtime([i["code"] for i in POOL])
    alerts = []
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
                alerts.append("🔴 <font color=\"red\">**【%s】现价 %.3f %s关键位 %.3f**</font>\n**动作：%s**（%s【%s】）" % (
                    name, r["price"], "跌破" if lv["type"] == "down" else "上破",
                    lv["price"], lv["desc"], lv.get("note", ""), lv.get("ref", "")))
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
                if no.startswith("B") and sg["kong"]:
                    act += "（但空头排列，只观察不执行）"
                alerts.append("🔴 <font color=\"red\">**【%s】触发%s【%s %s】**</font>\n现价 %.3f（%+.2f%%）" % (
                    name, act, no, desc, r["price"], r["chg"]))
    save_state(state)
    if not alerts:
        print("无新触发")
        return None
    t = datetime.datetime.now(BJ).strftime("%H:%M")
    return "## 触发告警 %s\n\n" % t + "\n\n---\n\n".join(alerts)


# ---------------- 入口 ----------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "intraday"
    now = datetime.datetime.now(BJ).strftime("%m-%d %H:%M")
    try:
        if not is_trading_day():
            print("[%s] 今日休市（行情时间戳不是今天），静默退出" % now)
            sys.exit(0)
        if not in_window(mode):
            print("[%s] 非有效时段，静默退出防误报（mode=%s）" % (now, mode))
            sys.exit(0)
        if mode == "intraday":
            push("【策略池对策】" + now, build_intraday(now))
        elif mode == "close":
            push("【收盘复盘+明日预案】" + now, build_close())
        elif mode == "monitor":
            content = build_monitor()
            if content:
                push("【触发告警】" + now, content)
        else:
            print("未知模式:", mode)
    except Exception:
        import traceback
        err = traceback.format_exc()
        print(err)
        push("【系统异常】" + now, "监控脚本出错：\n\n```\n" + err[-800:] + "\n```")
        raise
