# -*- coding: utf-8 -*-
"""中证2000 ETF 数据管道：补抓沪市 5 只产品的历史份额与净值。

为什么单独一个脚本
------------------
主篮子（BASKET，16 只沪市宽基）只服务于「国家队净流入」口径，中证2000
不在其中。穿透率需要它，但直接加进 BASKET 会改变净流入基线、污染 11.84 年
历史结论 ⇒ 因此单独建 data/pen_extra.json，只给穿透率用，不碰主口径。

数据来源
--------
份额：上交所官方日频接口（T+1 公布），一次可查一日全量 927 只，按代码筛。
净值：新浪 ETF 日线（sh + 代码），与主篮子同源，保证口径一致。

用法
----
python scripts/fetch_pen_extra.py                # 增量补抓最近 20 个工作日
python scripts/fetch_pen_extra.py --backfill 400 # 首次回补约 400 个工作日
python scripts/fetch_pen_extra.py --dry-run
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "pen_extra.json")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/156.0.0.0 Safari/537.36"
SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_SQLID = "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
SSE_HEAD = {"Referer": "https://www.sse.com.cn/", "User-Agent": UA}
SINA = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?symbol=sh%s&scale=240&ma=no&datalen=%d")

# 中证2000 的沪市跟踪产品（上交所全量接口实测 5 只，2026-11-20）
CODES = ["563300", "562660", "560220", "561370", "563200"]


def http_json(url, params=None, headers=None, timeout=25):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def sse_day(dt: str):
    """返回 {code: 份额(份)}；非交易日/未公布返回 None。"""
    day = dt[:4] + "-" + dt[4:6] + "-" + dt[6:]
    p = {"isPagination": "true", "pageHelp.pageSize": "10000",
         "pageHelp.pageNo": "1", "pageHelp.beginPage": "1",
         "pageHelp.cacheSize": "1", "pageHelp.endPage": "1",
         "sqlId": SSE_SQLID, "STAT_DATE": day}
    try:
        j = http_json(SSE_URL, p, SSE_HEAD, 30)
    except Exception:
        return dt, None
    res = j.get("result") or []
    if not res:
        return dt, None
    row = {}
    for x in res:
        c = x.get("SEC_CODE")
        if c in CODES:
            try:
                row[c] = float(x.get("TOT_VOL")) * 10000.0
            except (TypeError, ValueError):
                pass
    return dt, (row or None)


def workdays_back(n: int):
    bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    out, d = [], bj
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= datetime.timedelta(days=1)
    return out


def fetch_nav(datalen=1200):
    """新浪日线 → {code: {day: close}}，close 即单位净值近似（ETF 用收盘价）。"""
    out = {}
    for c in CODES:
        try:
            j = http_json(SINA % (c, datalen), None,
                          {"Referer": "https://finance.sina.com.cn", "User-Agent": UA}, 30)
        except Exception:
            print("  净值抓取失败", c, flush=True)
            continue
        if not isinstance(j, list):
            continue
        ser = {}
        for it in j:
            d = str(it.get("day") or "").replace("-", "")
            try:
                ser[d] = float(it.get("close"))
            except (TypeError, ValueError):
                pass
        if ser:
            out[c] = ser
            print("  %s 净值 %d 天（%s~%s）" % (c, len(ser), min(ser), max(ser)), flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=20, help="往回抓多少个工作日")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    old = {"shares": {}, "nav": {}}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            pass
    shares = old.get("shares") or {}
    have = set(shares.keys())

    days = workdays_back(a.backfill)
    todo = [d for d in days if d not in have or not shares.get(d)]
    print("目标 %d 个工作日，需抓 %d 个（已有 %d 天）" % (len(days), len(todo), len(have)), flush=True)

    got = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(sse_day, d): d for d in todo}
        for fu in as_completed(futs):
            d, row = fu.result()
            if row:
                shares[d] = row
                got += 1
    print("份额：新增 %d 天" % got, flush=True)

    nav = old.get("nav") or {}
    fresh = fetch_nav()
    for c, ser in fresh.items():
        nav.setdefault(c, {}).update(ser)

    if a.dry_run:
        print("[dry-run] 不写文件")
        return 0

    days_sorted = sorted(shares.keys())
    payload = {"codes": CODES, "shares": shares, "nav": nav,
               "asof": days_sorted[-1] if days_sorted else "",
               "built": (datetime.datetime.utcnow() + datetime.timedelta(hours=8))
                        .strftime("%Y-%m-%d %H:%M:%S")}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    os.replace(tmp, OUT)
    print("已写入 %s（%d 天，asof %s）" % (OUT, len(days_sorted), payload["asof"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
