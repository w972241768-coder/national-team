#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国家队持仓 · 数据更新管线（GitHub Actions 自包含，不依赖本地通达信文件）

数据链：
  上交所 ETF 份额日频接口（T 日份额于次日公布）  → data/shares.json（基线 + 增量）
  新浪财经日线（前复权）                        → data/nav.json + 上证指数
  Σ(份额变动 × 当日价格) = 净流入；再做分位、风格、簇化、分区间统计
  → docs/data.json + docs/state.json

容错铁律：任何一步抓不到数就保留基线并写 warning，**绝不写坏数据覆盖线上**。
输出前做 sanity check，不合格直接退出 1（CI 变红，Pages 继续显示上一版）。
"""
import os
import sys
import json
import math
import time
import bisect
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
DOCS = os.path.join(ROOT, "docs")

SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_HEAD = {"Referer": "https://www.sse.com.cn/", "User-Agent": "Mozilla/5.0"}
SSE_SQLID = "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
SINA_KLINE = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "CN_MarketData.getKLineData")
SINA_HEAD = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}

# 16 只沪市宽基，全部纳入净流入计算
BASKET = {
    "510300": ("沪深300", "300ETF华泰柏瑞"), "510310": ("沪深300", "HS300ETF易方达"),
    "510330": ("沪深300", "华夏300"), "515330": ("沪深300", "天弘300"),
    "515390": ("沪深300", "华安300"), "510360": ("沪深300", "广发300"),
    "510380": ("沪深300", "国寿300"), "510320": ("沪深300", "300中金"),
    "510050": ("上证50", "50ETF华夏"), "510210": ("上证指数", "综指ETF富国"),
    "510500": ("中证500", "500ETF南方"), "512100": ("中证1000", "1000ETF南方"),
    "512050": ("中证A500", "A500E华夏"), "563360": ("中证A500", "A500基金华泰柏瑞"),
    "588000": ("科创50", "科创50华夏"), "588080": ("科创50", "科创板50易方达"),
}
# 深市 9 只 + 560010：深交所无公开日频份额接口，且 560010 未纳入篮子 ⇒ 仅提供实时行情
QUOTE_ONLY = {
    "159919": ("沪深300", "300ETF嘉实"), "159915": ("创业板指", "创业板ETF易方达"),
    "159949": ("创业板50", "创业板50ETF华安"), "159952": ("创业板指", "创业板ETF广发"),
    "159338": ("中证A500", "A500ETF国泰"), "159352": ("中证A500", "A500ETF南方"),
    "159361": ("中证A500", "A500ETF易方达"), "159633": ("中证1000", "1000ETF易方达"),
    "159845": ("中证1000", "1000ETF华夏"), "560010": ("中证1000", "1000ETF广发"),
}
STYLES = ["沪深300", "上证50", "中证500", "中证1000", "中证A500", "科创50", "上证指数"]
C300 = [c for c in BASKET if BASKET[c][0] == "沪深300"]

WARNINGS = []


def warn(msg):
    WARNINGS.append(msg)
    print("  [WARN] " + msg, flush=True)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception as e:
        warn("读取 %s 失败：%s" % (os.path.basename(path), e))
        return default


def http_json(url, params=None, headers=None, timeout=25, retries=2):
    for k in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if k == retries:
                return {"__error__": "%s: %s" % (type(e).__name__, str(e)[:100])}
            time.sleep(1.0 + k)
    return {"__error__": "unknown"}


# ---------------------------------------------------------------- 抓取层
def sse_one_day(dt):
    p = {"isPagination": "true", "pageHelp.pageSize": "10000",
         "pageHelp.pageNo": "1", "pageHelp.beginPage": "1",
         "pageHelp.cacheSize": "1", "pageHelp.endPage": "1",
         "sqlId": SSE_SQLID, "STAT_DATE": dt}
    j = http_json(SSE_URL, params=p, headers=SSE_HEAD, timeout=30, retries=1)
    if "__error__" in j:
        return dt, None
    res = j.get("result", [])
    if not res:
        return dt, {"_holiday": True}          # 当日份额尚未公布 / 非交易日
    row = {}
    for x in res:
        c = x.get("SEC_CODE")
        if c in BASKET:
            try:
                row[c] = float(x.get("TOT_VOL")) * 10000.0   # 万份 → 份
            except (TypeError, ValueError):
                pass
    return dt, (row if row else {"_holiday": True})


def recent_workdays(n=12):
    """从北京今天往回列 n 个工作日（不判节假日，交由份额接口判空）"""
    bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    out = []
    d = bj
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= datetime.timedelta(days=1)
    return out


def update_shares(base):
    days = recent_workdays(12)
    # 未公布（_holiday）的日期要每天重试，否则份额次日公布后永远补不回来
    todo = [d for d in days
            if d not in base or (isinstance(base[d], dict) and base[d].get("_holiday"))]
    if not todo:
        print("  份额：最近 12 个工作日已全部缓存，无需抓取")
        return base, 0
    print("  份额：待补 %d 天（%s ~ %s）" % (len(todo), min(todo), max(todo)), flush=True)
    ok, failed = 0, []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(sse_one_day, d): d for d in todo}
        for fu in as_completed(futs):
            dt, row = fu.result()
            if row is None:
                failed.append(dt)
                continue
            base[dt] = row
            if not row.get("_holiday"):
                ok += 1
    if failed:
        warn("上交所 %d/%d 天请求失败（多因份额未公布，或境外 runner 不可达），保持基线：%s"
             % (len(failed), len(todo), ",".join(sorted(failed))))
    return base, ok


def sina_kline(symbol, datalen):
    u = "%s?symbol=%s&scale=240&ma=no&datalen=%d" % (SINA_KLINE, symbol, datalen)
    j = http_json(u, headers=SINA_HEAD, timeout=25, retries=2)
    if "__error__" in j or not isinstance(j, list):
        return symbol, {}
    out = {}
    for x in j:
        try:
            out[x["day"].replace("-", "")] = float(x["close"])
        except (KeyError, TypeError, ValueError):
            continue
    return symbol, out


def update_nav(base):
    print("  价格：抓 16 只 ETF 最近 30 根日线（增量）", flush=True)
    for fu in as_completed([_SUBMIT_NAV(c) for c in BASKET]):
        code, ser = fu.result()
        if ser:
            base.setdefault(code, {}).update(ser)
        else:
            warn("新浪 %s 价格抓取失败，沿用基线" % code)
    return base


_SUBMIT_NAV = None                    # 由 main 注入并发执行器


def fetch_index():
    print("  指数：抓上证综指全历史日线", flush=True)
    sym, ser = sina_kline("sh000001", 3000)
    if not ser:
        warn("新浪上证指数抓取失败")
    return ser


# ---------------------------------------------------------------- 计算层
def quantile(a, q):
    s = sorted(a)
    return s[min(len(s) - 1, int(len(s) * q))] if s else None


def pct_rank(a, v):
    return bisect.bisect_left(sorted(a), v) / len(a) * 100 if a else None


def build(sh, nav, idx):
    clean = {k: v for k, v in sh.items()
             if isinstance(v, dict) and "_err" not in v and "_holiday" not in v and v}
    days = sorted(d for d in clean if d in idx)
    rows, prev, first_seen = [], None, {}
    for d in days:
        cur = clean[d]
        nf_all = nf300 = aum = aum300 = 0.0
        by = {k: 0.0 for k in STYLES}
        if prev is not None:
            for c, (style, _n) in BASKET.items():
                s1, s0, p = cur.get(c), prev.get(c), nav.get(c, {}).get(d)
                if s1 is None or s0 is None or not p:
                    continue
                v = (s1 - s0) * p / 1e8
                nf_all += v
                by[style] += v
                if style == "沪深300":
                    nf300 += v
        for c in BASKET:
            s1, p = cur.get(c), nav.get(c, {}).get(d)
            if s1 and p:
                aum += s1 * p / 1e8
                if BASKET[c][0] == "沪深300":
                    aum300 += s1 * p / 1e8
                first_seen.setdefault(c, d)
        if prev is not None:
            rows.append({"d": d, "nf": nf_all, "nf300": nf300,
                         "aum": aum, "aum300": aum300, "idx": idx[d], "by": by})
        prev = cur
    return rows, first_seen


def fwd(rows, i, k):
    if i + 1 + k >= len(rows):
        return None
    return (rows[i + 1 + k]["idx"] / rows[i + 1]["idx"] - 1) * 100


def st(v):
    v = [x for x in v if x is not None]
    if not v:
        return [0, 0.0, 0.0, 0.0]
    s = sorted(v)
    return [len(v), sum(v) / len(v), s[len(s) // 2],
            sum(1 for x in v if x > 0) / len(v) * 100]


def clus(ii, gap=20):
    if not ii:
        return []
    out = [[ii[0]]]
    for i in ii[1:]:
        if i - out[-1][-1] >= gap:
            out.append([i])
        else:
            out[-1].append(i)
    return out


def analyze(rows, key, thr_key, pct_key):
    for i, r in enumerate(rows):
        w = [x[key] for x in rows[max(0, i - 250):i]]
        r[thr_key] = quantile(w, .95) if len(w) >= 250 else None
        r[pct_key] = pct_rank(w, r[key]) if len(w) >= 250 else None


def quint(rows, pctkey):
    valid = sorted((r[pctkey], i) for i, r in enumerate(rows) if r[pctkey] is not None)
    if not valid:
        return []
    q = len(valid) // 5
    out = []
    for b in range(5):
        seg = valid[b * q:(b + 1) * q] if b < 4 else valid[4 * q:]
        ii = [i for _, i in seg]
        it = {"label": "%d~%d%%" % (b * 20, (b + 1) * 20), "n": len(ii),
              "t5": st([fwd(rows, i, 5) for i in ii]),
              "t20": st([fwd(rows, i, 20) for i in ii])}
        out.append(it)
    return out


def rule_stats(rows, f):
    ii = [i for i, r in enumerate(rows) if f(r)]
    cc = clus(ii)
    a = st([fwd(rows, i, 20) for i in ii])
    b = st([fwd(rows, i, 20) for i in [x[0] for x in cc]])
    return {"events": a[0], "all": a[1], "win": a[3],
            "clusters": len(cc), "cluster_mean": b[1], "cluster_win": b[3]}


def periods(rows):
    segs = [("2015-2019", "20150101", "20191231"), ("2020-2022", "20200101", "20221231"),
            ("2023-2024/11", "20230101", "20241103"),
            ("2024/11-2026/11", "20241104", "20261103")]
    out = []
    for tag, a, b in segs:
        sub = [r for r in rows if a <= r["d"] <= b]
        if len(sub) < 80:
            continue
        bs = st([fwd(sub, i, 20) for i in range(len(sub))])
        rec = {"label": tag, "days": len(sub), "base": bs[1], "base_win": bs[3]}
        for key, f in (("pct", lambda r: r.get("sup")),
                       ("amt150", lambda r: r["nf"] > 150),
                       ("amt100_300", lambda r: r["nf300"] > 100)):
            r = rule_stats(sub, f)
            rec[key] = {"n": r["clusters"], "mean": r["cluster_mean"], "win": r["cluster_win"]}
        out.append(rec)
    return out


def threshold_scan(rows):
    out = []
    for th in (50, 80, 100, 150, 200, 300):
        out.append({"th": th,
                    "all": rule_stats(rows, lambda r, t=th: r["nf"] > t),
                    "c300": rule_stats(rows, lambda r, t=th: r["nf300"] > t)})
    return out


def pct_scan(rows):
    out = []
    for th in (80, 90, 95, 98, 99):
        f = lambda r, t=th: (r.get("pct") is not None and r["pct"] >= t and r["nf"] > 0)
        out.append({"th": th, **rule_stats(rows, f)})
    return out


def main():
    os.makedirs(DOCS, exist_ok=True)
    print("国家队持仓 · 数据更新开始", flush=True)

    shares = load_json(os.path.join(DATA, "shares.json"), {})
    nav = load_json(os.path.join(DATA, "nav.json"), {})
    print("  基线：份额 %d 天 / 价格 %d 只" % (len(shares), len(nav)), flush=True)
    if len(shares) < 2000 or len(nav) < 10:
        print("[FATAL] 基线数据缺失，终止", flush=True)
        sys.exit(1)

    shares, got = update_shares(shares)
    global _SUBMIT_NAV
    _ex = ThreadPoolExecutor(max_workers=6)
    _SUBMIT_NAV = lambda c: _ex.submit(sina_kline, "sh" + c, 30)
    nav = update_nav(nav)
    _ex.shutdown(wait=True)
    idx = fetch_index()

    # 若新浪指数整体失败，绝不能让 rows 变空
    if len(idx) < 1000:
        print("[FATAL] 上证指数序列不足（%d 根），终止以免写坏线上数据" % len(idx), flush=True)
        sys.exit(1)

    rows, first_seen = build(shares, nav, idx)
    print("  计算：%d 个交易日 %s -> %s" % (len(rows), rows[0]["d"], rows[-1]["d"]), flush=True)

    # sanity check
    last = rows[-1]
    if len(rows) < 2000 or not (2000 < last["idx"] < 7000) or abs(last["nf"]) > 5000:
        print("[FATAL] sanity check 不通过（rows=%d idx=%.1f nf=%.1f），终止"
              % (len(rows), last["idx"], last["nf"]), flush=True)
        sys.exit(1)

    analyze(rows, "nf", "thr", "pct")
    analyze(rows, "nf300", "thr300", "pct300")
    for r in rows:
        r["inten"] = r["nf"] / r["aum"] * 100 if r["aum"] else 0.0
        r["sup"] = bool(r.get("thr") is not None and r["nf"] > r["thr"] and r["nf"] > 0)
        r["sup300"] = bool(r.get("thr300") is not None and r["nf300"] > r["thr300"]
                           and r["nf300"] > 0)

    base = st([fwd(rows, i, 20) for i in range(len(rows))])
    rules = {
        "pct_all": rule_stats(rows, lambda r: r["sup"]),
        "pct_300": rule_stats(rows, lambda r: r["sup300"]),
        "fx100_all": rule_stats(rows, lambda r: r["nf"] > 100),
        "fx100_300": rule_stats(rows, lambda r: r["nf300"] > 100),
        "fx150_all": rule_stats(rows, lambda r: r["nf"] > 150),
    }

    # 精简 rows：历史行不带逐日风格明细（前端只用月度聚合），体积可压掉一半
    slim = []
    for r in rows:
        slim.append({"d": r["d"], "nf": round(r["nf"], 2), "nf300": round(r["nf300"], 2),
                     "aum": round(r["aum"], 1), "idx": round(r["idx"], 2),
                     "pct": None if r.get("pct") is None else round(r["pct"], 1),
                     "sup": r["sup"], "inten": round(r["inten"], 3)})

    last20 = rows[-20:]
    style20 = {s: round(sum(r["by"][s] for r in last20), 1) for s in STYLES}
    style_all = {s: round(sum(r["by"][s] for r in rows), 0) for s in STYLES}
    month_map = {}
    for r in rows:
        m = r["d"][:6]
        if m not in month_map:
            month_map[m] = {s: 0.0 for s in STYLES}
        for s in STYLES:
            month_map[m][s] += r["by"][s]
    style_month = [{"m": m, "by": {s: round(v, 1) for s, v in month_map[m].items()}}
                   for m in sorted(month_map)]

    payload = {
        "asof": last["d"],
        "sample_days": len(rows),
        "base": {"t20": base[1], "win": base[3]},
        "kpi": {
            "nf": round(last["nf"], 1), "nf300": round(last["nf300"], 1),
            "aum": round(last["aum"], 0), "inten": round(last["inten"], 2),
            "pct": round(last.get("pct") or 0, 1), "thr": round(last.get("thr") or 0, 1),
            "idx": round(last["idx"], 2),
            "by": {k: round(v, 1) for k, v in last["by"].items()},
            "style20": style20, "style_all": style_all,
            "sum20": round(sum(r["nf"] for r in last20), 0),
            "share300_20": round(style20["沪深300"] / max(1e-9, sum(r["nf"] for r in last20)) * 100, 1),
        },
        "rows": slim,
        "styles": STYLES,
        "style_month": style_month,
        "quint_all": quint(rows, "pct"),
        "quint_300": quint(rows, "pct300"),
        "rules": rules,
        "periods": periods(rows),
        "scan": threshold_scan(rows),
        "pct_scan": pct_scan(rows),
        "basket": [{"code": c, "style": v[0], "name": v[1], "from": first_seen.get(c)}
                   for c, v in BASKET.items()],
        "quotes": ([{"code": c, "style": v[0], "name": v[1], "grp": "in"}
                    for c, v in BASKET.items()] +
                   [{"code": c, "style": v[0], "name": v[1], "grp": "out"}
                    for c, v in QUOTE_ONLY.items()]),
    }

    # 写基线（增量回写，下次 CI 不必重抓）
    json.dump(shares, open(os.path.join(DATA, "shares.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    json.dump(nav, open(os.path.join(DATA, "nav.json"), "w", encoding="utf-8"),
              ensure_ascii=False)

    tmp = os.path.join(DOCS, "data.json.tmp")
    json.dump(payload, open(tmp, "w", encoding="utf-8"), ensure_ascii=False,
              separators=(",", ":"))
    os.replace(tmp, os.path.join(DOCS, "data.json"))

    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    state = {
        "gen": now.strftime("%Y%m%d-%H%M%S"),
        "asof": last["d"], "rows": len(rows),
        "built_cst": now.strftime("%Y-%m-%d %H:%M CST"),
        "source": "sse" if got else "baseline",
        "warnings": WARNINGS,
    }
    json.dump(state, open(os.path.join(DOCS, "state.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    print("\n完成：%s | 净流入 %+.1f 亿 | 样本 %d 天 | 新增份额 %d 天 | 警告 %d 条"
          % (last["d"], last["nf"], len(rows), got, len(WARNINGS)), flush=True)
    for w in WARNINGS:
        print("   ! " + w, flush=True)


if __name__ == "__main__":
    main()
