# -*- coding: utf-8 -*-
"""倍量选股扫描（2026-11-25 建，依据强龙【b76 倍量法则】）

用户要求："每天给我推荐一个这样的股票"

法则（缺一条就是假的）：
  ① 缩量期必须【横盘】，不是下跌（价格贴 MA60 ±8% 内横着，日均额稳定）
  ② 启动日【量涨价涨】：量 ≥3 倍基准 且 比前日 ≥3 倍，价格涨，收盘站上 MA60
  ③ 堆量期要【连续】：最近 10 日里 ≥5 日成交额 ≥5 倍基准，一天都不能掉回基准

候选池：东财量比排行（量比>2 的才看，因为堆量的票当量一定大）

用法：
  python tools/scan_beiliang.py            # 扫一遍，输出候选
  python tools/scan_beiliang.py --top 60   # 只扫前 60 只
"""
import json
import sys
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.eastmoney.com/"}
SINA = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d")


def get(u, tries=3):
    for i in range(tries):
        try:
            r = urllib.request.Request(u, headers=UA)
            return urllib.request.urlopen(r, timeout=15).read().decode("utf-8", "ignore")
        except Exception:
            time.sleep(1.5)
    return ""


def candidates(top=300):
    """新浪成交额排行（堆量的票当日成交额必然靠前）

    2026-11-25 换源：东财排行接口当晚被限流，新浪这个稳定。
    """
    out, seen = [], set()
    page = 1
    while len(out) < top and page <= 4:
        u = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "Market_Center.getHQNodeData?page=%d&num=80&sort=amount&asc=0&node=hs_a"
             "&symbol=&_s_r_a=page" % page)
        t = get(u)
        if not t.strip().startswith("["):
            break
        try:
            arr = json.loads(t)
        except Exception:
            break
        if not arr:
            break
        for x in arr:
            sym = str(x.get("symbol") or "")
            code = str(x.get("code") or "")
            if not sym or code in seen:
                continue
            # 排除 ST / 退市 / 北交所
            nm = str(x.get("name") or "")
            if "ST" in nm or "退" in nm or code.startswith(("4", "8", "9")):
                continue
            try:
                amt = float(x.get("amount") or 0) / 1e8
            except Exception:
                amt = 0
            if amt < 2:          # 成交额太小的不看（堆量至少要 2 亿）
                continue
            seen.add(code)
            out.append({"code": code, "sym": sym, "name": nm,
                        "pct": float(x.get("changepercent") or 0), "amt": amt})
        page += 1
        time.sleep(0.4)
    return out[:top]


def kline(sym, n=80):
    t = get(SINA % (sym, n))
    if not t or not t.strip().startswith("["):
        return []
    try:
        d = json.loads(t)
    except Exception:
        return []
    rows = []
    for x in d:
        try:
            c = float(x["close"])
            v = float(x["volume"])
            rows.append({"day": x["day"], "close": c, "amt": c * v / 1e8})
        except Exception:
            continue
    return rows


def _median(x):
    y = sorted(x)
    n = len(y)
    return y[n // 2] if n % 2 else (y[n // 2 - 1] + y[n // 2]) / 2


def _pct(x, q):
    """分位数（q 取 0-100），线性插值"""
    y = sorted(x)
    if not y:
        return 0
    k = (len(y) - 1) * q / 100.0
    lo, hi = int(k), min(int(k) + 1, len(y) - 1)
    return y[lo] + (y[hi] - y[lo]) * (k - lo)


def _cv(x):
    m = _median(x)
    if m <= 0:
        return 99
    mu = sum(x) / len(x)
    var = sum((v - mu) ** 2 for v in x) / len(x)
    return (var ** 0.5) / m


def check(rows, verbose=False):
    """倍量判定（2026-11-26 改为"区间法"，不用环比）

    核心：比较【新旧两个区间】是否分离，而不是比较两个点。
      ① 旧平台（前20日）：中位数 B、90分位 H_old、变异系数 CV<0.8
      ② 新平台（近10日）：中位数 N ≥ 5B、10分位 L_new ≥ 3B
      ③ 区间分离：L_new / H_old ≥ 2  ← 换挡的铁证
      ④ 启动日：amt≥3B 且 amt/前日≥3 且 价格涨 且 收盘>MA60
    """
    n = len(rows)
    if n < 45:
        return False, {"why": "数据不足"}
    cl = [r["close"] for r in rows]
    amt = [r["amt"] for r in rows]
    i = n - 1

    # ① 旧平台
    old = amt[max(0, i - 39):i - 19]
    if len(old) < 15:
        return False, {"why": "旧平台数据不足"}
    B = _median(old)
    H_old = _pct(old, 90)
    cv_old = _cv(old)
    if B <= 0:
        return False, {"why": "基准为0"}
    if cv_old > 0.8:
        return False, {"why": "旧平台太乱 CV=%.2f" % cv_old, "cv": cv_old}

    # ② 新平台
    new = amt[i - 9:i + 1]
    N = _median(new)
    L_new = _pct(new, 10)
    if N / B < 4:
        return False, {"why": "抬升不够 %.1fx" % (N / B), "nb": round(N / B, 1), "cv": cv_old}
    if L_new < B * 2.5:
        return False, {"why": "有一天掉回去了 %.1fx" % (L_new / B), "nb": round(N / B, 1), "cv": cv_old}

    # ③ 区间分离
    sep = L_new / H_old if H_old > 0 else 0
    if sep < 2:
        return False, {"why": "区间没分离 %.1fx（只是放大不是换挡）" % sep,
                       "nb": round(N / B, 1), "sep": round(sep, 1), "cv": cv_old}

    # ④ 启动日
    launch = None
    for k in range(max(1, i - 14), i + 1):
        if amt[k] >= 3 * B and amt[k] / max(amt[k - 1], 1e-6) >= 2 and cl[k] > cl[k - 1]:
            m60 = sum(cl[max(0, k - 59):k + 1]) / len(cl[max(0, k - 59):k + 1])
            if cl[k] > m60:
                launch = k
                break
    if launch is None:
        return False, {"why": "找不到量涨价涨的启动日", "nb": round(N / B, 1),
                       "sep": round(sep, 1), "cv": cv_old}

    ma60 = sum(cl[max(0, i - 59):i + 1]) / len(cl[max(0, i - 59):i + 1])
    px = cl[i]
    hold = sum(1 for a in new if a >= 5 * B)
    dev = (px / ma60 - 1) * 100
    ok = px > ma60
    return ok, {
        "base": round(B, 2), "new_med": round(N, 2), "nb": round(N / B, 1),
        "L_new": round(L_new, 2), "H_old": round(H_old, 2), "sep": round(sep, 1),
        "cv": round(cv_old, 2), "hold": hold, "launch": rows[launch]["day"],
        "launch_x": round(amt[launch] / B, 1),
        "px": round(px, 2), "ma60": round(ma60, 2), "dev": round(dev, 1),
        "pullback": abs(dev) < 8,
        "why": "OK" if ok else "价格在MA60下方",
    }


def main():
    top = 80
    if "--top" in sys.argv:
        top = int(sys.argv[sys.argv.index("--top") + 1])
    cand = candidates(top)
    print("候选（量比>2）：%d 只" % len(cand))
    hits = []
    for k, c in enumerate(cand):
        rows = kline(c["sym"])
        ok, d = check(rows)
        if ok:
            hits.append((c, d))
            print("  ✅ %s %s %.2f | 距MA60 %+.1f%% | 旧%.2f亿→新%.2f亿(%.1fx) | 区间分离%.1fx | 堆量%d日 | 启动%s(%.1fx) | %s"
                  % (c["code"], c["name"], d["px"], d["dev"], d["base"], d["new_med"],
                     d["nb"], d["sep"], d["hold"], d["launch"], d["launch_x"],
                     "★回踩买点区" if d["pullback"] else ""))
        if k % 10 == 0:
            time.sleep(0.3)
    print()
    if not hits:
        print("今日无符合条件的票（缩量横盘→堆量→站上MA60 三条全过）")
    else:
        hits.sort(key=lambda x: (-x[1]["hold"], x[1]["dev"]))
        print("=== 最像强龙样本的 ===")
        for c, d in hits[:5]:
            print("%s %s | 堆量%d日 | 距MA60 %+.1f%% | 旧%.2f亿 → 新%.2f亿（%.1f倍）| 区间分离%.1fx"
                  % (c["code"], c["name"], d["hold"], d["dev"], d["base"],
                     d["new_med"], d["nb"], d["sep"]))
    # 2026-11-26：更新跟踪记录并打印 10 日涨幅
    try:
        t = track_update(hits)
        print()
        print("===== 倍量跟踪 =====")
        block = track_block(t)
        for ln in block:
            print(ln)
        if CLOUD:
            import datetime as _dt
            md = ("## 🔍 倍量选股（b76 区间法）\n\n**更新**：%s\n\n"
                  % _dt.datetime.now().strftime("%Y-%m-%d %H:%M")) + "\n".join(block) + \
                 "\n\n> 四条判据：旧平台CV<0.8 ／ 新中位数≥5倍 ／ 区间分离≥2 ／ 启动日量涨价涨+站上MA60\n"
            io_ = __import__("io")
            io_.open(OUT_MD, "w", encoding="utf-8").write(md)
            print("已写", OUT_MD)
    except Exception as e:
        print("跟踪更新失败:", str(e)[:80])
    return 0


# ===== 跟踪：记录入选后 10 个交易日的涨幅（2026-11-26 用户要求）=====
import os as _os
CLOUD = "--cloud" in __import__("sys").argv
TRACK = ("state/beiliang_track.json" if CLOUD else ".secrets/beiliang_track.json")
OUT_MD = "beiliang_view.md" if CLOUD else ".secrets/beiliang_view.md"
FOLLOW_N = 10


def _load_track():
    import os
    if not os.path.exists(TRACK):
        return {"picks": []}
    try:
        return json.load(open(TRACK, encoding="utf-8"))
    except Exception:
        return {"picks": []}


def _save_track(t):
    import os
    os.makedirs(os.path.dirname(TRACK), exist_ok=True)
    json.dump(t, open(TRACK, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _cum10(sym, start_day, n=FOLLOW_N):
    """从 start_day 起第 n 个交易日的涨幅（%），不足 n 日返回 (涨幅, 已跟踪天数)"""
    rows = kline(sym, 240)
    if not rows:
        return None, 0
    days = [r["day"] for r in rows]
    if start_day not in days:
        return None, 0
    i0 = days.index(start_day)
    tail = rows[i0:]
    if len(tail) < 2:
        return 0.0, 0
    p0 = tail[0]["close"]
    k = min(n, len(tail) - 1)
    p1 = tail[k]["close"]
    return round((p1 / p0 - 1) * 100, 2), k


def track_update(today_hits):
    """把今天的入选写进记录，并回算每一条的 10 日涨幅"""
    t = _load_track()
    have = {(p["date"], p["code"]) for p in t["picks"]}
    import datetime
    today = datetime.date.today().isoformat()
    for c, d in today_hits:
        if (today, c["code"]) in have:
            continue
        t["picks"].append({"date": today, "code": c["code"], "sym": c["sym"],
                           "name": c["name"], "price": d["px"], "nb": d["nb"],
                           "sep": d["sep"], "hold": d["hold"], "launch": d["launch"]})
    for p in t["picks"]:
        pct, done = _cum10(p["sym"], p["date"])
        p["pct"] = pct
        p["done"] = done
        p["finished"] = done >= FOLLOW_N
    _save_track(t)
    return t


def track_block(t):
    out = []
    today = __import__("datetime").date.today().isoformat()
    today_picks = [p for p in t["picks"] if p["date"] == today]
    if today_picks:
        out.append("**今日入选 %d 只**" % len(today_picks))
        for p in today_picks:
            out.append("- **%s %s** %.2f ｜ 旧→新 %.1f倍 ｜ 区间分离 %.1fx ｜ 堆量 %d 日"
                       % (p["code"], p["name"], p["price"], p["nb"], p["sep"], p["hold"]))
    else:
        out.append("**今日入选：0 只**（150 只活跃股无一只满足四条判据）")
    hist = [p for p in t["picks"] if p["date"] != today]
    if hist:
        out.append("")
        out.append("**历史跟踪（入选后 N 个交易日）**")
        out.append("")
        out.append("| 入选日 | 标的 | 入选价 | 现价涨幅 | 已跟踪 | 状态 |")
        out.append("|---|---|---|---|---|---|")
        for p in sorted(hist, key=lambda x: x["date"], reverse=True)[:8]:
            pct = p.get("pct")
            ptxt = ("%+.1f%%" % pct) if pct is not None else "—"
            st = "✅完成" if p.get("finished") else "跟踪中"
            out.append("| %s | %s %s | %.2f | %s | %d/%d | %s |"
                       % (p["date"][5:], p["code"], p["name"], p["price"], ptxt,
                          p.get("done") or 0, FOLLOW_N, st))
    return out


if __name__ == "__main__":
    sys.exit(main())
