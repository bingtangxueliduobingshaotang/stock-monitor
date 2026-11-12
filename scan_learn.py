# -*- coding: utf-8 -*-
"""云端学习扫描（2026-11-12 建）——解决"不开机也能知道有没有新课"

为什么单独写：
    本地的 `daily_learn_check.py` 依赖 tools/ 目录、transcripts 目录和浏览器，
    云端跑不了。这个脚本只做一件事：**扫 B站 4 家的动态，对比 learned.json，
    有没学过的新视频就推微信**。转写和精读仍然在本机（需要 GPU 和我读稿）。

抖音为什么不在里面：
    抖音的列表接口要 X-Bogus 签名，`dy_fetch.py` 是靠本机 Chrome 调试端口拿的，
    GitHub Actions 上没有浏览器，扫不了。抖音只能开机后补扫。

环境变量：
    BILI_COOKIE   B站登录态整串 cookie（GitHub Secret）
    PUSHPLUS_TOKEN
    DAYS          只看最近几天（默认 3）

用法：python scan_learn.py
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
LEARNED = os.path.join(BASE, "learned.json")
UPS = [
    ("强龙", 3537112695572818),
    ("教主", 256764901),
    ("饕餮", 257540556),
    ("邹老师", 3493257352842037),
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def http(url, cookie="", tries=5):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Cookie": cookie,
                                                       "Referer": "https://space.bilibili.com/"})
            return json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore"))
        except Exception:
            time.sleep(2 + i * 2)
    return {}


def learned():
    try:
        return set(json.load(open(LEARNED, encoding="utf-8")).get("bvid") or [])
    except Exception:
        return set()


def is_paid(bv, cookie):
    d = http("https://api.bilibili.com/x/web-interface/view?bvid=%s" % bv, cookie, tries=3).get("data") or {}
    return bool(d.get("is_upower_exclusive"))


def days_ago(pub, n):
    """pub_time 形如 '2026-11-12 09:30' / '昨天 22:00' / '3小时前' / '11月6日'"""
    import datetime
    now = datetime.datetime.now()
    t = (pub or "").strip()
    try:
        if "天前" in t:
            return int(t.replace("天前", "").strip()) <= n
        if "小时前" in t or "分钟前" in t or t.startswith("刚刚"):
            return True
        if t.startswith("昨天"):
            return n >= 1
        if t.startswith("前天"):
            return n >= 2
        if "月" in t and "日" in t:
            s = t.replace("月", "-").replace("日", "")
            mm, dd = s.split("-")[:2]
            d = datetime.date(now.year, int(mm), int(dd))
            return (now.date() - d).days <= n
        d = datetime.datetime.strptime(t[:10], "%Y-%m-%d")
        return (now - d).days <= n
    except Exception:
        return True


def main():
    cookie = os.environ.get("BILI_COOKIE", "").strip()
    days = int(os.environ.get("DAYS") or 3)
    done = learned()
    report, total_new = [], 0
    for name, mid in UPS:
        items, ok = [], False
        for i in range(8):                      # 空结果重试（-352 风控，实测第 7 次才成）
            d = http("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?host_mid=%d" % mid,
                     cookie, tries=1).get("data") or {}
            items = d.get("items") or []
            if items:
                ok = True
                break
            time.sleep(2 + i * 2)
        if not ok:
            report.append("**%s**：接口失败（可能漏学）" % name)
            continue
        new = []
        for x in items:
            m = x.get("modules", {})
            a = (m.get("module_dynamic", {}).get("major") or {}).get("archive") or {}
            bv = a.get("bvid")
            if not bv or bv in done:
                continue
            pub = (m.get("module_author") or {}).get("pub_time", "")
            if not days_ago(pub, days):
                continue
            if is_paid(bv, cookie):
                continue
            new.append((bv, pub, (a.get("title") or "")[:40]))
        total_new += len(new)
        if new:
            report.append("**%s** %d 条：\n" % (name, len(new)) +
                          "\n".join("  · %s ｜ %s ｜ %s" % (bv, pub, ti) for bv, pub, ti in new))
        else:
            report.append("**%s**：无新课" % name)
    # 日志脱敏：本仓库是 public，Actions 日志全网可见。
    # 只输出"几家有新课"，UP 主名字和视频标题只进微信推送，不进日志。
    _ok = sum(1 for r in report if "无新课" in r)
    _fail = sum(1 for r in report if "接口失败" in r)
    print("scan done: %d 家无新课 | %d 家有新课 | %d 家接口失败 | 合计新视频 %d 条"
          % (_ok, len(report) - _ok - _fail, _fail, total_new))
    if total_new:
        body = ("**B站有 %d 条没学的新视频**（近 %d 天）\n\n%s\n\n"
                "---\n开机后跟我说一声「学习」，我把它们一次学完（含抖音 3 家）。\n"
                "注：抖音的列表接口需要本机浏览器，云端扫不到，开机后补。"
                % (total_new, days, "\n\n".join(report)))
        tok = os.environ.get("PUSHPLUS_TOKEN", "").strip()
        if tok:
            data = json.dumps({"token": tok, "title": "📚 学习提醒：B站 %d 条新课" % total_new,
                               "content": body, "template": "markdown"}).encode()
            req = urllib.request.Request("https://www.pushplus.plus/send", data=data,
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=20).read()
                print("已推送提醒")
            except Exception as e:
                print("推送失败:", e)
        else:
            print("[WARN] 无 PUSHPLUS_TOKEN")
    else:
        print("近期无新课")
    return 0


if __name__ == "__main__":
    sys.exit(main())
