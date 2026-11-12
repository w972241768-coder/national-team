# -*- coding: utf-8 -*-
"""结论前置：把「一眼结论」硬编码进 docs/index.html。

设计意图（2026-11-10 手机端故障复盘）：
  data.json 约 340 KB，移动端弱网 / 内置浏览器一旦该请求超时或被拦，
  原启动逻辑的 render() 完全不执行，整页停在「载入中…」。
  因此把最关键的数字与研判直接写进 HTML，不依赖任何外部请求；
  联网成功时再由 JS 用 data.json 里同一份 verdict 覆盖，兼顾实时与兜底。

本模块被 update.py 调用：make_verdict() 产出结构 → 同时写入 data.json
（供 JS 在线覆盖）与 index.html 的 VERDICT 标记块（供离线显示）。
"""
import json
import os
import re

START = "<!--VERDICT:START-->"
END = "<!--VERDICT:END-->"
HIST_START = "<!--HIST:START-->"
HIST_END = "<!--HIST:END-->"


def hist_block(rows):
    """全历史日度序列压成紧凑数组 [d, nf, nf300, idx] 内嵌，零请求即可缩放。

    2877 天 × 4 个数约 80 KB；比对象数组（304 KB）小 4 倍，且离线全部可用。
    """
    arr = [[str(r["d"]), r["nf"], r["nf300"], r["idx"]] for r in rows]
    txt = json.dumps(arr, ensure_ascii=False, separators=(",", ":"))
    return (HIST_START + "\n" + '<script id="ntHist" type="application/json">' +
            txt + '</script>\n' + HIST_END)


def _s(v, d=1, sign=False):
    if v is None:
        return "--"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "--"
    p = ("+" if sign and x > 0 else "") + ("%.*f" % (d, x))
    return p


def _cls(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    return "up" if x > 0 else ("down" if x < 0 else "")


def make_verdict(payload, state):
    """由完整统计结果生成结论对象。判定只用已确认份额日的数据，不用盘中行情。"""
    k = payload.get("kpi", {})
    rows = payload.get("rows", [])
    nf = k.get("nf", 0) or 0
    nf300 = k.get("nf300", 0) or 0
    inten = k.get("inten", 0) or 0
    pct = k.get("pct", 0) or 0
    thr = k.get("thr", 0) or 0
    aum = k.get("aum", 0) or 0
    idx = k.get("idx", 0) or 0
    sum20 = k.get("sum20", 0) or 0
    share300 = k.get("share300_20")

    # 方向判定：以「单日净流入 vs 滚动 95 分位阈值」+「强度」为主，近 20 日累计只作背景
    if thr > 0 and nf >= thr:
        tone, title = "bull", "偏多 · 大额净流入"
        lead = "单日放量流入，已触发滚动 95 分位"
    elif nf > 0 and inten >= 0.3:
        tone, title = "bull", "偏多 · 温和净流入"
        lead = "资金温和回补，未触发高分位"
    elif (thr > 0 and nf <= -thr * 0.5) or inten <= -0.5:
        tone, title = "bear", "偏空 · 明显流出"
        lead = "单日流出放大，资金结构转弱"
    elif nf < 0:
        tone, title = "neutral", "中性偏弱 · 小幅流出"
        lead = "单日小幅流出，不构成买卖信号"
    elif nf > 0:
        tone, title = "neutral", "中性偏多 · 小幅流入"
        lead = "单日小幅流入，量级未构成信号"
    else:
        tone, title = "neutral", "中性 · 资金平稳"
        lead = "当日份额基本持平"

    phase = "回补中" if sum20 > 0 else ("流出中" if sum20 < 0 else "持平")

    # 动能衰减识别：前三日均为大额净流入、末一日转负且倒数第二日已缩到不足前三日最小值 20%
    r6 = []
    for r in rows[-6:]:
        try:
            r6.append(float(r.get("nf", 0) or 0))
        except (TypeError, ValueError):
            r6.append(0.0)
    trend = ""
    if (len(r6) >= 6 and all(x > 50 for x in r6[:3]) and r6[-1] <= 0
            and abs(r6[-2]) < min(r6[:3]) * 0.2):
        trend = ("近 6 日 %s → %s <b>连续衰减</b>，资金动能明显转弱，"
                 "需观察下一确认日是否转为持续流出"
                 % (" / ".join(_s(x, 0, True) for x in r6[:3]),
                    " / ".join(_s(x, 0, True) for x in r6[3:])))
    tail = trend or (("单日流出属高位回补后的正常喘息，未触发放量阈值")
                     if nf < 0 and (thr <= 0 or nf > -thr * 0.5)
                     else "请结合下文的分区间稳定性复核，勿据单日数值外推")

    desc = (
        "全宽基 16 只单日净流入 <b>%s 亿</b>（强度 %s%%，处滚动 250 日 <b>%s%%</b> 分位，"
        "95%% 分位阈值 %s 亿），沪深300 八只 <b>%s 亿</b>；近 20 日累计 <b>%s 亿</b>，"
        "资金相位<b>%s</b>。%s。"
        % (_s(nf, 1, True), _s(inten, 2), _s(pct, 1), _s(thr, 1),
           _s(nf300, 1, True), _s(sum20, 0, True), phase, tail)
    )

    action = (
        "若下一份额公布日全宽基净流入重回 <b>+%s 亿</b>以上、且沪深300 同步转正，"
        "则视为资金再度回补、可维持现有敞口观察；否则维持<b>%s</b>判断，"
        "不因单日数据加减仓。资金结构<b>只作观察，不作买卖点</b>。"
        % (_s(thr or 200, 0), title.split(" · ")[0])
    )

    metrics = [
        {"lab": "全宽基 16 只 · 当日净流入", "val": _s(nf, 1, True) + " 亿",
         "cls": _cls(nf), "sub": "250日分位 %s%% · 95%%阈值 %s 亿" % (_s(pct, 1), _s(thr, 1))},
        {"lab": "沪深300 八只 · 当日净流入", "val": _s(nf300, 1, True) + " 亿",
         "cls": _cls(nf300),
         "sub": ("该子篮近20日规模占比 %s%%" % _s(share300, 1)) if share300 is not None else "份额口径确认值"},
        {"lab": "篮子规模 / 强度", "val": _s(aum, 0) + " 亿", "cls": "",
         "sub": "%s%% · 上证 %s" % (_s(inten, 2), _s(idx, 2))},
        {"lab": "近 20 日累计净流入", "val": _s(sum20, 0, True) + " 亿",
         "cls": _cls(sum20), "sub": "资金相位：%s" % phase},
    ]

    recent = [{"d": r.get("d"), "nf": r.get("nf", 0)} for r in rows[-6:] if r.get("d")]
    if not recent and rows:
        recent = [{"d": rows[-1].get("d"), "nf": rows[-1].get("nf", 0)}]

    return {
        "asof": str(state.get("asof") or payload.get("asof") or "--"),
        "built": state.get("built_cst", "--"),
        "source": state.get("source", "--"),
        "warnings": list(state.get("warnings") or []),
        "tone": tone,
        "title": title,
        "lead": lead,
        "nf": nf, "aum": aum,
        "desc": desc,
        "action": action,
        "metrics": metrics,
        "recent": recent,
    }


def _recent_html(v):
    out = []
    for r in v.get("recent", []):
        d = str(r.get("d") or "")
        ds = d[4:6] + "-" + d[6:8] if len(d) == 8 else d
        nf = r.get("nf", 0) or 0
        out.append('<span class="vr"><i>%s</i> <b class="%s">%s</b></span>'
                   % (ds, _cls(nf), _s(nf, 1, True)))
    return "".join(out)


def render_html(v):
    """生成静态结论卡 HTML。与前端 paintVerdict() 的结构保持一致。"""
    ms = []
    for m in v.get("metrics", []):
        ms.append('<div class="vm"><span>%s</span><b class="%s">%s</b><em>%s</em></div>'
                  % (m.get("lab", ""), m.get("cls", ""), m.get("val", "--"), m.get("sub", "")))
    warn = ""
    if v.get("warnings"):
        warn = ('<div class="vwarn">本次取数有 %d 条告警（多为无新增份额日或境外 runner 受限），'
                '下方数值为<b>最近一个已确认份额日</b>的结论，未受影响。</div>'
                % len(v["warnings"]))
    return (
        '<section class="verdict %s" id="verdict">\n'
        '  <div class="vhead"><div class="vlab">研判结论 <span class="vtag">已确认 · 份额口径 T+1</span></div>\n'
        '    <div class="vdate">份额数据日 <b>%s</b> · 文件生成 %s</div></div>\n'
        '  <div class="vmain"><div class="vtitle">%s<span>%s</span></div>\n'
        '    <div class="vdesc">%s</div></div>\n'
        '  <div class="vgrid">%s</div>\n'
        '  <div class="vrecent"><span class="dim">近 6 个已确认交易日（亿）：</span>%s</div>\n'
        '  <div class="vact"><b>怎么做</b>：%s</div>\n'
        '  %s'
        '  <div class="vfoot">数据来源：上交所官方日频份额（T+1）、新浪日线与成交额基准、腾讯实时快照。'
        '结论为公开数据研究，仅用于观察资金结构，不构成投资建议，不承诺收益或准确率，不自动下单；'
        '接口存在延迟、缺失或中断可能。<button class="btn vdiag" onclick="toggleDiag()">🔍 自检</button></div>\n'
        '  <pre id="diag" class="diag" style="display:none"></pre>\n'
        '</section>\n'
        % (v.get("tone", "neutral"), _d8(v.get("asof")), v.get("built", "--"),
           v.get("title", "--"), v.get("lead", ""), v.get("desc", ""),
           "".join(ms), _recent_html(v), v.get("action", ""), warn)
    )


def _d8(s):
    s = str(s or "")
    return s[:4] + "-" + s[4:6] + "-" + s[6:8] if len(s) == 8 else s


def embed_verdict(v, html_path, quotes=None, lite=None, hist_rows=None):
    """幂等替换 index.html 里的 VERDICT 块，并内嵌精简快照供 JS 覆盖使用。

    quotes（26 只代码表，约 2 KB）一并内嵌：即使 data.json 完全不可用，
    实时行情与分时曲线仍能靠浏览器直连腾讯跑起来，不必等历史数据。

    lite = 去掉 rows 的完整统计结果（约 39 KB）。手机弱网时 343 KB 的
    data.json 常常超时，导致 KPI / 所有明细表格一并空白；把轻版明细
    直接写进 HTML，页面一打开就有具体数据，联网后再用 data.json 覆盖。
    """
    html = open(html_path, "r", encoding="utf-8").read()
    if START not in html or END not in html:
        raise RuntimeError("index.html 缺少 VERDICT 标记块，无法内嵌结论")
    snap_obj = {"verdict": v, "quotes": quotes or []}
    if lite:
        snap_obj["lite"] = lite
    snap = json.dumps(snap_obj, ensure_ascii=False, separators=(",", ":"))
    # 防止数据里出现 </script> 提前闭合内嵌 JSON 块
    snap = snap.replace("</", "<\\/")
    block = (START + "\n" + render_html(v) +
             '<script id="ntSnapshot" type="application/json">' + snap + '</script>\n' + END)
    new = re.sub(re.escape(START) + r".*?" + re.escape(END),
                 lambda _m: block, html, count=1, flags=re.S)
    # 曲线序列一并内嵌（页面零请求即可画出近 4 年并缩放）。缺标记则静默跳过，
    # 保持对旧版 index.html 的兼容。
    if hist_rows and HIST_START in new and HIST_END in new:
        hb = hist_block(hist_rows)
        new = re.sub(re.escape(HIST_START) + r".*?" + re.escape(HIST_END),
                     lambda _m: hb, new, count=1, flags=re.S)
    if new == html:
        return False
    tmp = html_path + ".tmp"
    open(tmp, "w", encoding="utf-8").write(new)
    os.replace(tmp, html_path)
    return True
