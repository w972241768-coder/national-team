# -*- coding: utf-8 -*-
"""本地重建：用已有的 data.json / history.json 重算 verdict 并重嵌 index.html。

用途（2026-11-23「本期反常 + 归因视角」改造）：
  线上 index.html 的 VERDICT / HIST 静态块由 CI 写回，本地只改模板不会上线。
  本脚本不重新抓取上交所份额（避免复现不了 CI 的份额基线），只做三件事：
    ① 用现有 data.json(kpi) + history.json(rows) + state.json 重算 verdict；
    ② 把 verdict + 轻版明细 + 全历史曲线（含 aum）重新内嵌进 index.html；
    ③ 同步写 data.json，并把版本号（ver.json / BUILD_ID / 标题版本）一起推进。

用法：
  python scripts/rebuild_docs.py                 # 直接写盘
  python scripts/rebuild_docs.py --dry-run       # 只算不写，打印 verdict 关键字段
  python scripts/rebuild_docs.py --amv 132075.8 --amv-date 2026-11-23   # 手给 0AMV（不联网）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCS = os.path.join(ROOT, "docs")
sys.path.insert(0, HERE)

import verdict as V  # noqa: E402


def bj_now():
    return datetime.now(timezone.utc) + timedelta(hours=8)


def load(name):
    with open(os.path.join(DOCS, name), "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--amv", type=float, default=None, help="0AMV 当日值（亿元），给了就不联网")
    ap.add_argument("--amv-date", default="", help="0AMV 交易日 YYYY-MM-DD")
    ap.add_argument("--no-bump", action="store_true", help="不推进版本号（仅重算内容）")
    a = ap.parse_args()

    lite = load("data.json")
    hist = load("history.json")
    state = load("state.json")
    rows = hist.get("rows") or []
    if not rows:
        sys.exit("history.json 没有 rows，无法重算")

    payload = dict(lite)
    payload["rows"] = rows

    amv = None
    if a.amv is not None:
        amv = {"v": a.amv, "pct": None, "d": a.amv_date, "state": "", "src": "手工给定"}
    else:
        amv = V.fetch_amv()
        print("  0AMV：%s" % (json.dumps(amv, ensure_ascii=False) if amv else "取数失败（页面降级）"))

    v = V.make_verdict(payload, state, amv=amv)
    old = lite.get("verdict") or {}
    print("  份额数据日：%s（%s）" % (v["asof"], v["built"]))
    print("  判定码：%s" % v["anomaly_code"])
    print("  本期反常：%s" % re.sub(r"</?b>", "", v["anomaly"] or "（空）"))
    if v.get("pen"):
        print("  篮子÷0AMV：%.2f%%（分母±10%% ⇒ %.2f%%~%.2f%%）"
              % (v["pen"]["pct"], v["pen"]["lo"], v["pen"]["hi"]))
    print("  desc/action 与线上一致：%s / %s"
          % (v["desc"] == old.get("desc"), v["action"] == old.get("action")))

    if a.dry_run:
        return

    payload["verdict"] = v
    lite_new = {k: val for k, val in payload.items() if k != "rows"}
    tmp = os.path.join(DOCS, "data.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lite_new, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, os.path.join(DOCS, "data.json"))

    changed = V.embed_verdict(v, os.path.join(DOCS, "index.html"),
                              lite.get("quotes") or [], lite_new, rows)
    print("  index.html：%s" % ("已重嵌" if changed else "内容无变化"))

    if not a.no_bump:
        build = bj_now().strftime("%Y%m%d-%H%M")
        with open(os.path.join(DOCS, "ver.json"), "w", encoding="utf-8") as f:
            json.dump({"build": build}, f)
        p = os.path.join(DOCS, "index.html")
        html = open(p, "r", encoding="utf-8").read()
        m = re.search(r'var BUILD_ID="([^"]+)"', html)
        if m and m.group(1) != build:
            html = html.replace('var BUILD_ID="%s"' % m.group(1),
                                'var BUILD_ID="%s"' % build)
            html = html.replace("· 版本 %s" % m.group(1), "· 版本 %s" % build)
            open(p, "w", encoding="utf-8").write(html)
            print("  版本号：%s → %s" % (m.group(1), build))


if __name__ == "__main__":
    main()
