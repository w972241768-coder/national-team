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
# 仅提供实时行情、不计入净流入的 10 只，分两种原因（页面分组标题会分别标注）：
#   ① 深市 9 只 —— 深交所无公开日频份额接口，取不到历史份额；
#   ② 560010 —— **沪市**（56 开头，上交所接口确实返回其份额），只是未纳入本篮子历史基线。
#      它不是「深市无接口」，措辞不要混为一谈。
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
    # 防御性加固：接口当前同时接受 20261103 与 2026-11-03（实测两种都返回 923 条、
    # 510300 份额一致），但官方文档口径是 YYYY-MM-DD；统一成带连字符，防止将来收紧校验。
    day = dt[:4] + "-" + dt[4:6] + "-" + dt[6:] if len(dt) == 8 and "-" not in dt else dt
    p = {"isPagination": "true", "pageHelp.pageSize": "10000",
         "pageHelp.pageNo": "1", "pageHelp.beginPage": "1",
         "pageHelp.cacheSize": "1", "pageHelp.endPage": "1",
         "sqlId": SSE_SQLID, "STAT_DATE": day}
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
            # ★ 2026-11-04（专家审查指出）：上交所 T 日份额于 T+1 才公布，
            #   因此「今天」查回来必然是空数组，sse_one_day 会返回 {"_holiday": True}。
            #   若照写进 shares.json（已入库），每次跑脚本都会产生一条无意义的 git diff。
            #   近 2 天内的空结果不写盘 ⇒ 该日期仍不在 base 里 ⇒ todo 每天自动重试，
            #   份额公布后自然补上；自愈能力不变，但不会再污染缓存文件。
            #   ⚠️ 不采纳「>3 天仍为空就固化 _holiday」：那会把接口临时故障的
            #      真实交易日永久误标成节假日，比 git diff 严重得多。
            if row.get("_holiday"):
                try:
                    _bj = (datetime.datetime.utcnow()
                           + datetime.timedelta(hours=8)).date()
                    _gap = (_bj - datetime.datetime.strptime(dt, "%Y%m%d").date()).days
                    if _gap <= 2:
                        continue
                except (TypeError, ValueError):
                    pass
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
        sym, ser = fu.result()
        # sina_kline 返回带市场前缀的 symbol（sh510300），而 build() 用纯代码
        # （510300）取价。曾把 symbol 直接当 code 写回，增量价格全落进另一组键，
        # 计算层永远只读到旧基线 ⇒ 11-05 起净流入/规模被算成 0（KPI 一片 0）。
        # 这里统一去掉前缀，确保增量真正并进纯代码序列。
        code = sym[2:] if sym[:2] in ("sh", "sz") else sym
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


def fetch_amount_bench(last_d):
    """近 20 个交易日「全部 26 只 ETF 合计成交额」基准（亿元，估算值）。

    新浪日线 volume 单位为股，amount ≈ volume × close / 1e8。
    实测 510300 于 2026-11-04：估算 127.23 亿 vs 腾讯快照 125.62 亿，误差 +1.3%，
    作为「放量倍数」的参照系够用，但必须在页面上标注为估算。
    ⚠️ 盘中浏览器的今日值是「截至当前时刻累计」，与这里的「全日」均值不可直接相除，
       前端只在快照时间已过 15:00 时才显示倍数。
    """
    all_codes = list(BASKET) + list(QUOTE_ONLY)
    # 两套口径必须分开：页面「26 只实时合计成交额」与「16 只篮内分时累计」
    # 各自要用同口径的均值做分母，否则会出现「分子 16 只 / 分母 26 只」的错配。
    daily, daily_in, missing = {}, {}, []
    for code in all_codes:
        sym = ("sh" if code[0] in "56" else "sz") + code
        u = "%s?symbol=%s&scale=240&ma=no&datalen=25" % (SINA_KLINE, sym)
        j = http_json(u, headers=SINA_HEAD, timeout=25, retries=1)
        if "__error__" in j or not isinstance(j, list):
            missing.append(code)
            continue
        is_in = code in BASKET
        for x in j:
            try:
                d = x["day"].replace("-", "")
                v = float(x["volume"]) * float(x["close"]) / 1e8
                daily.setdefault(d, 0.0)
                daily[d] += v
                if is_in:
                    daily_in.setdefault(d, 0.0)
                    daily_in[d] += v
            except (KeyError, TypeError, ValueError):
                pass
    # 不能用 sorted(daily)[-21:-1] 无条件砍掉最后一天：
    #   · 盘前 08:30 / 周末运行时，最后一根是昨天已完整收盘的 K 线，砍掉会白丢一天；
    #   · 盘中运行时，最后一根可能是当日未收盘的半截成交额，必须排除。
    # 统一以「最新已确认份额日 last_d」为上界，取 <= last_d 的最近 20 个交易日，
    # 口径与页面 asof 一致，且不依赖运行时段。
    valid_days = [d for d in sorted(daily) if d <= last_d][-20:]
    vals = [daily[d] for d in valid_days]
    vals_in = [daily_in.get(d, 0.0) for d in valid_days]
    if len(vals) < 10:
        return None
    return {"avg20": round(sum(vals) / len(vals), 1),          # 26 只全口径
            "avg20_in": round(sum(vals_in) / len(vals_in), 1),  # 16 只篮内口径
            "n_all": len(all_codes) - len(missing), "n_in": len(BASKET),
            "prev": round(vals[-1], 1) if vals else None,
            "days": len(vals), "last_day": valid_days[-1], "missing": missing,
            "method": "新浪日线成交量 × 收盘价（估算），实测误差约 +1.3%"}


def probe_sse(dt):
    """健康检查：用「最近一个已确认份额日」回访上交所接口。

    GitHub Actions 的 runner 在境外，能否访问 query.sse.com.cn 是整条自动更新链路的核心假设。
    结果写进 docs/state.json，便于事后核对，不必去翻需要鉴权的 Actions 日志。
    """
    day = dt[:4] + "-" + dt[4:6] + "-" + dt[6:]
    p = {"isPagination": "true", "pageHelp.pageSize": "10000",
         "pageHelp.pageNo": "1", "pageHelp.beginPage": "1",
         "pageHelp.cacheSize": "1", "pageHelp.endPage": "1",
         "sqlId": SSE_SQLID, "STAT_DATE": day}
    j = http_json(SSE_URL, params=p, headers=SSE_HEAD, timeout=30, retries=0)
    if "__error__" in j:
        return {"ok": False, "date": day, "error": j["__error__"][:140]}
    res = j.get("result", [])
    sample = next((x.get("TOT_VOL") for x in res if x.get("SEC_CODE") == "510300"), None)
    return {"ok": True, "date": day, "rows": len(res), "v510300": sample}


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
    # 最后一段的上界必须跟着数据走，否则新增交易日会被悄悄排除在统计之外。
    last_d = rows[-1]["d"]
    tail_label = "2024/11-%s/%s" % (last_d[:4], last_d[4:6])
    segs = [("2015-2019", "20150101", "20191231"), ("2020-2022", "20200101", "20221231"),
            ("2023-2024/11", "20230101", "20241103"),
            (tail_label, "20241104", last_d)]
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


def fund_flow_by_code(sh, nav, day_list):
    """单只 ETF 的当日 / 近 20 日净流入（亿元）。

    shares.json 保留了逐日逐只份额，所以这部分不需要额外取数。
    day_list 传最近 21 个有效交易日（20 个差额需要 21 个切片）。
    """
    out = {c: {"d1": 0.0, "d20": 0.0} for c in BASKET}
    for i in range(1, len(day_list)):
        d0, d1 = day_list[i - 1], day_list[i]
        for c in BASKET:
            s0, s1 = sh.get(d0, {}).get(c), sh.get(d1, {}).get(c)
            p = nav.get(c, {}).get(d1)
            if s0 is None or s1 is None or not p:
                continue
            v = (s1 - s0) * p / 1e8
            if i >= len(day_list) - 20:
                out[c]["d20"] += v
            if i == len(day_list) - 1:
                out[c]["d1"] += v
    return {c: {"d1": round(v["d1"], 1), "d20": round(v["d20"], 1)}
            for c, v in out.items()}


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
    sum20 = sum(r["nf"] for r in last20)
    style20 = {s: round(sum(r["by"][s] for r in last20), 1) for s in STYLES}
    style_all = {s: round(sum(r["by"][s] for r in rows), 0) for s in STYLES}
    daily_style = [{"d": r["d"], "by": {s: round(r["by"][s], 1) for s in STYLES}}
                   for r in last20]
    fund_flow = fund_flow_by_code(shares, nav, [r["d"] for r in rows[-21:]])
    # 历史单日净流入 Top 20，并标出「>100 亿事件」的簇起点。
    # 非簇起点的行额外带出所属簇的起点日期与间隔交易日数，避免读者困惑
    # 「为什么史上最大天量反而没有簇起点标签」——它往往是簇内第 2 天。
    big_clusters = clus([i for i, r in enumerate(rows) if r["nf"] > 100])
    big_starts = {c[0] for c in big_clusters}
    belong = {}
    for c in big_clusters:
        for i in c:
            belong[i] = (c[0], i - c[0])
    top_days = []
    for i, r in sorted(enumerate(rows), key=lambda x: -x[1]["nf"])[:20]:
        # ⚠️ 局部变量不能叫 st：模块级有统计函数 st()，同名会让整个 main() 内的 st 变成局部名，
        #    在 base = st([...]) 处抛 UnboundLocalError（项目里已踩过一次，勿再犯）。
        ci, off = belong.get(i, (None, None))
        top_days.append({"d": r["d"], "nf": round(r["nf"], 1),
                         "nf300": round(r["nf300"], 1), "idx": round(r["idx"], 2),
                         "pct": None if r.get("pct") is None else round(r["pct"], 1),
                         "start": i in big_starts,
                         "cluster_start": rows[ci]["d"] if ci is not None else None,
                         "cluster_offset": off})
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
            "sum20": round(sum20, 0),
            # 净流出、300 逆势流出、或 300 超过总额（其余风格净流出）时，占比都没有意义
            "share300_20": (round(style20["沪深300"] / sum20 * 100, 1)
                            if sum20 > 0 and 0 <= style20["沪深300"] <= sum20 else None),
        },
        "rows": slim,
        "styles": STYLES,
        "style_month": style_month,
        "daily_style": daily_style,
        "fund_flow": fund_flow,
        "top_days": top_days,
        "quint_all": quint(rows, "pct"),
        "quint_300": quint(rows, "pct300"),
        "rules": rules,
        "periods": periods(rows),
        "scan": threshold_scan(rows),
        "pct_scan": pct_scan(rows),
        "basket": [{"code": c, "style": v[0], "name": v[1], "from": first_seen.get(c),
                    "d1": fund_flow[c]["d1"], "d20": fund_flow[c]["d20"]}
                   for c, v in BASKET.items()],
        "quotes": ([{"code": c, "style": v[0], "name": v[1], "grp": "in"}
                    for c, v in BASKET.items()] +
                   [{"code": c, "style": v[0], "name": v[1], "grp": "out",
                     "reason": ("sse_not_in_baseline" if c == "560010" else "szse_no_api")}
                    for c, v in QUOTE_ONLY.items()]),
    }

    # 以下两项必须在写 data.json 之前算完并并入 payload，否则字段会静默丢失。
    probe = probe_sse(max(d for d, v in shares.items()
                          if isinstance(v, dict) and v and not v.get("_holiday")))
    print("  上交所健康检查：%s" % json.dumps(probe, ensure_ascii=False), flush=True)
    payload["sse_probe"] = probe

    amount_bench = fetch_amount_bench(last["d"])
    if amount_bench:
        print("  成交额基准：近 %d 日全日日均 %.1f 亿（26只全口径）/ %.1f 亿（16只篮内），缺失 %d 只"
              % (amount_bench["days"], amount_bench["avg20"],
                 amount_bench["avg20_in"], len(amount_bench["missing"])), flush=True)
    else:
        print("  成交额基准：数据不足，跳过（不影响主流程）", flush=True)
    payload["amount_bench"] = amount_bench

    # 写基线（增量回写，下次 CI 不必重抓）
    json.dump(shares, open(os.path.join(DATA, "shares.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    json.dump(nav, open(os.path.join(DATA, "nav.json"), "w", encoding="utf-8"),
              ensure_ascii=False)

    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    state = {
        "gen": now.strftime("%Y%m%d-%H%M%S"),
        "asof": last["d"], "rows": len(rows),
        "built_cst": now.strftime("%Y-%m-%d %H:%M CST"),
        "source": "sse" if got else "baseline",
        "sse_probe": probe,
        "warnings": WARNINGS,
    }

    # 结论前置：同一份 verdict 同时进 data.json（联网时 JS 覆盖）与
    # index.html 的静态块（手机弱网 / data.json 失败时仍能一眼看到结论）。
    from verdict import make_verdict, embed_verdict
    payload["verdict"] = make_verdict(payload, state)

    tmp = os.path.join(DOCS, "data.json.tmp")
    json.dump(payload, open(tmp, "w", encoding="utf-8"), ensure_ascii=False,
              separators=(",", ":"))
    os.replace(tmp, os.path.join(DOCS, "data.json"))
    json.dump(state, open(os.path.join(DOCS, "state.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    try:
        changed = embed_verdict(payload["verdict"], os.path.join(DOCS, "index.html"),
                                payload.get("quotes", []))
        print("  结论内嵌：%s" % ("已更新 index.html" if changed else "内容无变化"), flush=True)
    except Exception as e:
        warn("结论内嵌失败（不影响数据）：%s" % e)

    print("\n完成：%s | 净流入 %+.1f 亿 | 样本 %d 天 | 新增份额 %d 天 | 警告 %d 条"
          % (last["d"], last["nf"], len(rows), got, len(WARNINGS)), flush=True)
    for w in WARNINGS:
        print("   ! " + w, flush=True)


if __name__ == "__main__":
    main()
