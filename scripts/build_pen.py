# -*- coding: utf-8 -*-
"""真实 ETF 穿透率：用指数点位 × 校准除数还原指数自由流通市值日频序列。

为什么可以这样做
----------------
中证指数系列按「自由流通市值加权」编制，指数点位 = 自由流通总市值 ÷ 除数。
除数只在成分调整/自由流通股本变动时跳变，日内不变，因此：

    自由流通市值(t) ≈ 点位(t) × K

K 用权威锚点校准（本脚本取东吴证券 2026-09-20 报告表1 的 2026-09-07 数值），
再用同报告表2 的「均值 / 区间最低最高 / 期初 / 期末」五组统计量做交叉校验。

穿透率 = 跟踪该指数的 ETF 合计规模 ÷ 指数自由流通市值 × 100%
  分子：Σ(当日份额 × 当日单位净值)，只取沪市可得的份额（深市无公开日频接口）
  分母：点位 × K
  因分子只覆盖沪市部分产品，按锚点日的覆盖率折算到全量口径，折算系数固定。

输出
----
docs/pen.json  —— 供页面直接读取：
    {"asof":..., "anchor":"2026-09-07", "src":"...", "series":{指数:[{d,pen,etf,fmv,cov},...]}}
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCS = os.path.join(ROOT, "docs")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

# 锚点：东吴证券《被动化的边界》2026-09-20，表1（2026-09-07）
#   sse_etf = 沪市跟踪该指数的 ETF 合计规模（亿元）
#   fmv     = 该指数自由流通市值（亿元）
#   pen     = 期末穿透率（%）
#   stats   = 表2 的 均值/最低/最高/期初/期末，用于交叉校验
SPEC = [
    dict(name="沪深300", code="000300",
         etfs=["510300", "510310", "510330", "515330", "515390", "510360", "510380", "510320"],
         sse_etf=2332.4, fmv=236668.2, pen=0.99,
         stats=dict(mean=2.63, lo=0.76, hi=4.48, p0=4.47, p1=0.99)),
    dict(name="中证500", code="000905", etfs=["510500"],
         sse_etf=627.9, fmv=90051.3, pen=0.70,
         stats=dict(mean=1.29, lo=0.55, hi=2.05, p0=1.96, p1=0.70)),
    dict(name="中证1000", code="000852", etfs=["512100"],
         sse_etf=269.5, fmv=77984.8, pen=0.35,
         stats=dict(mean=0.78, lo=0.15, hi=1.54, p0=1.49, p1=0.35)),
]
ANCHOR_DAY = "20260907"
SAMPLE_START, SAMPLE_END = "20250901", "20260907"   # 研报样本期


def bj_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=8)


def index_perf(code: str, start: str, end: str) -> dict:
    url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
           "?indexCode=%s&startDate=%s&endDate=%s" % (code, start, end))
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://www.csindex.com.cn/"})
    with urllib.request.urlopen(req, timeout=25) as r:
        body = json.loads(r.read().decode("utf-8"))
    return {x["tradeDate"]: float(x["close"]) for x in body.get("data") or []}


def load(name: str) -> dict:
    with open(os.path.join(ROOT, name), "r", encoding="utf-8") as f:
        return json.load(f)


def build() -> dict:
    sh = load("data/shares.json")
    nav = load("data/nav.json")
    days = sorted(sh.keys())
    if not days:
        sys.exit("shares.json 为空")
    start = min(days)
    end = max(days)

    out = {"asof": "", "anchor": ANCHOR_DAY,
           "src": "份额×净值（沪市）÷ 指数自由流通市值（点位×校准除数）",
           "calib": "东吴证券《被动化的边界》2026-09-20 表1/表2", "series": {}}
    report = []

    for sp in SPEC:
        px = index_perf(sp["code"], start, end)
        if ANCHOR_DAY not in px:
            report.append("%s：锚点日无点位，跳过" % sp["name"])
            continue
        k = sp["fmv"] / px[ANCHOR_DAY]

        def scale(day: str) -> float:
            tot = 0.0
            for c in sp["etfs"]:
                s = sh.get(day, {}).get(c)
                n = nav.get(c, {}).get(day)
                if s and n:
                    tot += float(s) * float(n)
            return tot / 1e8

        cov = scale(ANCHOR_DAY) / sp["sse_etf"] if sp["sse_etf"] else 0
        if not (0.3 < cov <= 1.02):
            report.append("%s：覆盖率异常 %.3f，跳过" % (sp["name"], cov))
            continue

        rows = []
        for day in days:
            p = px.get(day)
            if not p:
                continue
            s = scale(day)
            if s <= 0:
                continue
            fmv = p * k
            pen = s / cov / fmv * 100
            rows.append({"d": day, "pen": round(pen, 4), "etf": round(s / cov, 1),
                         "fmv": round(fmv, 0), "cov": round(cov, 4)})
        if not rows:
            report.append("%s：无有效行" % sp["name"])
            continue

        win = [r["pen"] for r in rows if SAMPLE_START <= r["d"] <= SAMPLE_END]
        st = sp["stats"]
        got = dict(mean=sum(win) / len(win) if win else None,
                   lo=min(win) if win else None, hi=max(win) if win else None,
                   p0=win[0] if win else None, p1=win[-1] if win else None)
        chk = []
        for key, label, tol in (("mean", "均值", 0.10), ("lo", "最低", 0.05),
                                ("hi", "最高", 0.20), ("p0", "期初", 0.20), ("p1", "期末", 0.05)):
            if got[key] is None:
                chk.append("%s缺" % label)
                continue
            d = got[key] - st[key]
            chk.append("%s %.2f vs %.2f (%+.2f)%s" %
                       (label, got[key], st[key], d, "" if abs(d) <= tol else " ⚠"))
        report.append("%s：K=%.4f 覆盖 %.1f%% 有效 %d 日 | %s | 最新 %s %.2f%%" %
                      (sp["name"], k, cov * 100, len(rows), " / ".join(chk),
                       rows[-1]["d"], rows[-1]["pen"]))

        out["series"][sp["name"]] = rows

    if out["series"]:
        out["asof"] = max(r["d"] for rows in out["series"].values() for r in rows)
    out["built"] = bj_now().strftime("%Y-%m-%d %H:%M:%S")
    out["report"] = report
    return out


def compact(pen: dict, keep_days: int = 760) -> dict:
    """内嵌用紧凑版：每行 [日期, 穿透率, ETF规模, 自由流通市值]，只保留近 keep_days 个交易日。

    完整序列留在 docs/pen.json（云端按需读取），页面内嵌只放近 3 年，
    这样即使断网也能看到当前读数与近一年区间，且 HTML 体积可控（约 30 KB）。
    覆盖率 cov 与锚点信息单独放顶层，不逐行重复。
    """
    out = {"asof": pen.get("asof", ""), "anchor": pen.get("anchor", ""),
           "src": pen.get("src", ""), "calib": pen.get("calib", ""),
           "built": pen.get("built", ""), "cov": {}, "series": {}}
    for name, rows in (pen.get("series") or {}).items():
        if not rows:
            continue
        tail = rows[-keep_days:]
        out["cov"][name] = tail[-1].get("cov")
        out["series"][name] = [[r["d"], round(r["pen"], 2), round(r["etf"], 0),
                               round(r["fmv"], 0)] for r in tail]
    return out


def main() -> int:
    ap_ok = "--dry-run" in sys.argv
    data = build()
    print("\n".join(data.pop("report")))
    if not data["series"]:
        sys.exit("没有任何指数可用")
    if ap_ok:
        print("\n[dry-run] 不写文件，最新读数：")
        for k, rows in data["series"].items():
            print("  %s %s %.2f%%（ETF %.0f 亿 / 自由流通 %.0f 亿）" %
                  (k, rows[-1]["d"], rows[-1]["pen"], rows[-1]["etf"], rows[-1]["fmv"]))
        return 0
    os.makedirs(DOCS, exist_ok=True)
    p = os.path.join(DOCS, "pen.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(compact(data), f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    os.replace(tmp, p)
    print("\n已写入 %s（asof %s，%d 个指数，内嵌紧凑版）" %
          (p, data["asof"], len(data["series"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
