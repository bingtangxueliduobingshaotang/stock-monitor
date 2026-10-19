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
    if mode == "morning":
        return 915 <= t <= 940
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
def mail_fallback(title, content, pp_code=None):
    """邮件兜底通道：config.json 的 mail 段；发送失败只打日志，不影响主流程"""
    cfg = CFG.get("mail") or {}
    user, pwd, to = cfg.get("user"), cfg.get("pass"), cfg.get("to")
    if not (user and pwd and to):
        return
    import smtplib
    from email.mime.text import MIMEText
    from email.header import Header
    tag = "（PushPlus 未确认送达，邮件兜底）" if pp_code != 200 else ""
    body = content[:20000] + "\n\n---\n\n[通道] PushPlus 返回 %s%s" % (pp_code, tag)
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = user
        msg["To"] = to
        sv = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30)
        sv.login(user, pwd)
        sv.sendmail(user, [to], msg.as_string())
        sv.quit()
        print("MAIL: OK ->", to)
    except Exception as e:
        print("MAIL ERR:", str(e)[:100])


def push(title, content):
    if not TOKEN:
        print("[WARN] 未配置 PUSHPLUS_TOKEN，跳过 PushPlus，走邮件通道")
        mail_fallback(title, content, None)
        return
    st = load_state()
    st["count"] = st.get("count", 0) + 1
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
        print("PUSH:", code, msg)
    except Exception as e:
        print("PUSH ERR:", str(e)[:80])
    # 邮件兜底（PushPlus 不可靠时保证送达）
    mail_fallback(title, content, code)


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
    # 大盘关键位（config 的 market 段；2026-10-16 验证日布置）
    try:
        mkrt = realtime(["sh000001"]).get("sh000001")
        if mkrt:
            for lv in CFG.get("market", {}).get("levels", []):
                key = "market|level|%s|%s" % (lv["type"], lv["price"])
                st, fired = level_status(mkrt["price"], lv)
                if fired and key not in state["fired"]:
                    state["fired"].append(key)
                    alerts.append("🔴 <font color=\"red\">**【上证指数】现价 %.2f %s %.2f**</font>\n**动作：%s**（【%s】）" % (
                        mkrt["price"], "上破" if lv["type"] == "up" else "跌破",
                        lv["price"], lv["desc"], lv.get("ref", "")))
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
                    alerts.append("🔵 【中证银行】现价 %.2f **跌破 5 日线**（MA5 %.2f）\n**小寒右侧信号出现**：资金或开始从防御切换——关注科技/证券承接（Q8 框架）" % (bkrt["price"], ma5))
            else:
                if key in state["fired"]:
                    state["fired"].remove(key)
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
        if mode == "morning":
            push("【今日盯防】" + now, build_morning())
        elif mode == "intraday":
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
