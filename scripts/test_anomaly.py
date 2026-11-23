# -*- coding: utf-8 -*-
"""「本期反常」规则一致性校验：Python 生成端 ↔ 前端自检端。

为什么要有这个测试：
  该行文案只由 Python 生成（verdict.anomaly），前端另有一份判定阶梯只用来自检。
  两份实现一旦走偏，就会出现「页面自检报不一致」或「自检通过但文案是错的」——
  两者都等于把错误数据当结论。所以用同一批 fixture 同时跑两边，逐条比对判定码。

跑法：
  python scripts/test_anomaly.py           # 全绿即退出码 0
  python scripts/test_anomaly.py -v        # 打印每条 fixture 的判定码与文案
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import verdict as V  # noqa: E402

START = "/*ANOM_RULE:START*/"
END = "/*ANOM_RULE:END*/"


def base(**kw):
    i = {"nf": 0.0, "nf300": 0.0, "aum": 1000.0, "idx": 3000.0, "thr": 0.0,
         "pct": 50.0, "aum_prev": 1000.0, "idx_prev": 3000.0,
         "streak": 1, "streak_sum": 0.0, "streak_idx": None}
    i.update(kw)
    return i


FIXTURES = [
    # ① 真实 2026-11-19：连续 4 日逆势申购 780 亿，上证累计 −1.56%
    ("真实 11-19 · 连续逆势申购", base(
        nf=408.6, nf300=185.8, aum=8610.0, aum_prev=8179.2, idx=3577.62, idx_prev=3587.83,
        thr=203.9, pct=99.6, streak=4, streak_sum=780.28, streak_idx=-1.5609), "streak_div"),
    # ② 同样的单日数据但只连续 2 日 ⇒ 降级为单日背离
    ("单日反向 · 大额流入而指数跌", base(
        nf=408.6, nf300=185.8, aum=8610.0, aum_prev=8179.2, idx=3577.62, idx_prev=3587.83,
        thr=203.9, pct=99.6, streak=2, streak_sum=590.0, streak_idx=-1.2), "price_div"),
    # ③ 分母对冲：申购 100 亿但规模只动 5 亿
    ("假稳 · 分子分母相抵", base(
        nf=100.0, nf300=60.0, aum=1005.0, aum_prev=1000.0, idx=3000.0, idx_prev=3000.0,
        pct=90.0, thr=200.0), "offset"),
    # ④ 资金在进、规模在退
    ("规模与资金反向", base(
        nf=50.0, nf300=20.0, aum=970.0, aum_prev=1000.0, idx=3030.0, idx_prev=3000.0,
        pct=70.0, thr=100.0), "aum_div"),
    # ⑤ 总量与沪深300 子篮反向
    ("风格间分化", base(
        nf=20.0, nf300=-10.0, aum=1040.0, aum_prev=1000.0, idx=3000.0, idx_prev=3000.0,
        pct=60.0, thr=50.0), "style_div"),
    # ⑥ 低于「显著」门槛 ⇒ 不硬凑
    ("小额 · 不判反常", base(
        nf=5.0, nf300=3.0, aum=1001.0, aum_prev=1000.0, idx=2999.0, idx_prev=3000.0,
        pct=40.0, thr=203.9), "none"),
    # ⑦ 大额且三者同向 ⇒ 无背离
    ("大额同向 · 无背离", base(
        nf=300.0, nf300=200.0, aum=1300.0, aum_prev=1000.0, idx=3060.0, idx_prev=3000.0,
        pct=99.0, thr=203.9), "none"),
    # ⑧ 缺前一交易日 ⇒ 不下判断
    ("缺前值 · 不判", base(nf=100.0, aum_prev=None, idx_prev=None), "nodata"),
]


def py_codes():
    return [V.anom_code(i) for _, i, _ in FIXTURES]


def js_codes():
    html = open(os.path.join(ROOT, "docs", "index.html"), "r", encoding="utf-8").read()
    a, b = html.find(START), html.find(END)
    if a < 0 or b < 0:
        sys.exit("index.html 缺少 ANOM_RULE 标记块，无法做前端一致性校验")
    block = html[a + len(START):b]
    with tempfile.TemporaryDirectory() as td:
        bp = os.path.join(td, "rule.js")
        fp = os.path.join(td, "fx.json")
        hp = os.path.join(td, "run.js")
        open(bp, "w", encoding="utf-8").write(block)
        json.dump([{"inputs": i} for _, i, _ in FIXTURES], open(fp, "w", encoding="utf-8"))
        open(hp, "w", encoding="utf-8").write(
            "const fs=require('fs');\n"
            "const code=fs.readFileSync(process.argv[2],'utf8');\n"
            "const fn=new Function(code+'\\nreturn anomCode;')();\n"
            "const fx=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));\n"
            "console.log(JSON.stringify(fx.map(f=>fn(f.inputs))));\n")
        out = subprocess.run(["node", hp, bp, fp], capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("node 执行失败：%s" % (out.stderr.strip()[:400] or out.stdout.strip()[:400]))
    return json.loads(out.stdout.strip())


def main():
    verbose = "-v" in sys.argv
    pc, jc = py_codes(), js_codes()
    fails = []
    for (name, inputs, want), p, j in zip(FIXTURES, pc, jc):
        if p != want:
            fails.append("%s：Python 判定码 %s，期望 %s" % (name, p, want))
        if j != p:
            fails.append("%s：前端 %s ≠ Python %s" % (name, j, p))
        txt = V._anom_text(p, inputs)
        if p == "nodata" and txt:
            fails.append("%s：nodata 不应产出文案" % name)
        if p != "nodata":
            if not txt:
                fails.append("%s：文案为空" % name)
            if re.search(r"None|nan|NaN|%%", txt):
                fails.append("%s：文案含非法占位符" % name)
        if verbose:
            print("[%s] py=%s js=%s → %s" % (name, p, j, re.sub(r"</?b>", "", txt)))

    # 真实数据端到端：data.json + history.json 必须能跑出判定码而不是 nodata
    try:
        d = json.load(open(os.path.join(ROOT, "docs", "data.json"), encoding="utf-8"))
        h = json.load(open(os.path.join(ROOT, "docs", "history.json"), encoding="utf-8"))
        k = d.get("kpi") or {}
        i = V.anom_inputs(k, h.get("rows") or [])
        c = V.anom_code(i)
        if c == "nodata":
            fails.append("真实数据：判定为 nodata（kpi/rows 字段不全）")
        print("真实数据（%s）判定码：%s" % (d.get("asof"), c))
        print("真实文案：%s" % re.sub(r"</?b>", "", V._anom_text(c, i)))
    except FileNotFoundError:
        print("跳过真实数据校验（找不到 docs/data.json 或 history.json）")

    if fails:
        print("\n❌ 未通过（%d）：" % len(fails))
        for f in fails:
            print("  - " + f)
        return 1
    print("\n✅ 全部通过：Python 与前端判定码逐条一致（%d 条 fixture）" % len(FIXTURES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
