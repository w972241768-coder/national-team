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
import time
import urllib.request

START = "<!--VERDICT:START-->"
END = "<!--VERDICT:END-->"
HIST_START = "<!--HIST:START-->"
HIST_END = "<!--HIST:END-->"


def hist_block(rows):
    """全历史日度序列压成紧凑数组 [d, nf, nf300, idx, aum] 内嵌，零请求即可缩放。

    aum（篮子规模，亿）供「分子/分母归因」离线使用；4 字段旧版仍兼容（归因自动跳过）。
    """
    arr = [[str(r["d"]), r["nf"], r["nf300"], r["idx"],
            round(float(r["aum"]), 1) if r.get("aum") is not None else None]
           for r in rows]
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


# ============================ 本期反常（自动生成） ============================
# 设计原则（2026-11-23）：
#   ① 只由「当期数据」推出，不允许任何固定文案或人工填写；
#   ② 判断阶梯只此一份（Python 生成），前端另有一份「同阶梯」的 anomCode()
#      仅做一致性自检，文案永远只来自本模块；
#   ③ 数字与结论卡其他处同源（kpi 的 nf/aum/idx + 前一交易日 rows[-2]），
#      避免「一句话里的数和上面卡片对不上」。
# 阈值是可解释的经验值，改阈值必须同时跑 scripts/test_anomaly.py。
ANOM_MIN_ABS = 20.0        # 亿元：低于此额度不判定为「显著」，避免零头噪声
ANOM_THR_RATIO = 0.3       # 「大额」= 滚动 95 分位阈值 × 0.3
ANOM_SPLIT_ABS = 5.0       # 亿元：风格间背离的最小可读差额
ANOM_OFFSET_RATIO = 0.15   # |Δ规模| < |净流入| × 0.15 视为两变量对冲（假稳）

AMV_URL = "https://w972241768-coder.github.io/etf-daily/amv_live.json"


def fetch_amv(timeout=6):
    """取活跃市值（0AMV）当日值。

    口径：0AMV = Σ(活跃流通股本 × 当日价)，单位亿元，是「活筹市值」，
    恒等式 0号指数(全流通总市值) = 0AMV(活筹) + 0DMV(死筹)。
    ⚠ 它不是「自由流通市值」，页面必须如实标注（见 docs 口径核对卡）。
    拿不到则返回 None，任何上游都不因此失败。
    """
    try:
        req = urllib.request.Request(
            AMV_URL + "?t=%d" % int(time.time()),
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            o = json.loads(r.read().decode("utf-8"))
        if not isinstance(o, dict) or o.get("ok") is not True:
            return None
        v = float(o.get("value"))
        if not (0 < v < 1_000_000):
            return None
        pct = o.get("change_pct")
        return {"v": v,
                "pct": None if pct is None else float(pct),
                "d": str(o.get("trade_date") or o.get("updated_at") or ""),
                "state": str(o.get("state") or ""),
                "src": "zgnb.top"}
    except Exception:
        return None


def _fnum(x, default=None):
    if x is None:
        return default
    try:
        y = float(x)
    except (TypeError, ValueError):
        return default
    return default if y != y else y


def anom_inputs(k, rows):
    """收集判定所需的全部数值，供「判断」与「自检」共用同一份输入。"""
    i = {
        "nf": _fnum(k.get("nf"), 0.0),
        "nf300": _fnum(k.get("nf300"), 0.0),
        "aum": _fnum(k.get("aum"), 0.0),
        "idx": _fnum(k.get("idx"), 0.0),
        "thr": _fnum(k.get("thr"), 0.0),
        "pct": _fnum(k.get("pct"), 0.0),
        "aum_prev": None, "idx_prev": None,
        "streak": 0, "streak_sum": 0.0, "streak_idx": None,
    }
    rows = rows or []
    if len(rows) >= 2:
        i["aum_prev"] = _fnum(rows[-2].get("aum"))
        i["idx_prev"] = _fnum(rows[-2].get("idx"))
    # 连续同向天数（含当日），以及该串起点前一日 → 当日的指数累计涨跌
    nf = i["nf"]
    if rows and abs(nf) > 0:
        s = 0
        for r in reversed(rows):
            v = _fnum(r.get("nf"))
            if v is None or v * nf <= 0:
                break
            s += 1
        i["streak"] = s
        i["streak_sum"] = sum(_fnum(rows[-1 - j].get("nf"), 0.0) for j in range(s))
        base_pos = len(rows) - 1 - s
        if base_pos >= 0:
            base = _fnum(rows[base_pos].get("idx"))
            if base:
                i["streak_idx"] = (i["idx"] / base - 1) * 100
    return i


def anom_code(i):
    """判断阶梯（前端 anomCode() 必须与此逐条一致，由 test_anomaly.py 校验）。"""
    nf = i.get("nf")
    aum_p, idx_p = i.get("aum_prev"), i.get("idx_prev")
    if nf is None or not aum_p or not idx_p or not i.get("aum") or not i.get("idx"):
        return "nodata"
    big = abs(nf) >= max(abs(i.get("thr") or 0) * ANOM_THR_RATIO, ANOM_MIN_ABS)
    if not big:
        return "none"
    ic = (i["idx"] / idx_p - 1) * 100
    da = i["aum"] - aum_p
    # 多日同向逆势优先：同一现象里证据更足（连续 N 日 vs 单日）
    si = i.get("streak_idx")
    if (i.get("streak") or 0) >= 3 and i.get("streak_sum") and si is not None \
            and nf * si < 0 and abs(i["streak_sum"]) >= ANOM_MIN_ABS * 2:
        return "streak_div"
    if nf * ic < 0:
        return "price_div"
    if abs(da) < abs(nf) * ANOM_OFFSET_RATIO and abs(nf) >= ANOM_MIN_ABS:
        return "offset"
    if nf * da < 0 and abs(da) >= ANOM_MIN_ABS:
        return "aum_div"
    n300 = i.get("nf300")
    if n300 is not None and nf * n300 < 0 \
            and min(abs(nf), abs(n300)) >= ANOM_SPLIT_ABS:
        return "style_div"
    return "none"


def _anom_text(code, i):
    """文案：所有数字均来自 anom_inputs(i)，与结论卡同源同精度。"""
    nf, aum, aum_p, idx, idx_p = (i.get("nf"), i.get("aum"), i.get("aum_prev"),
                                  i.get("idx"), i.get("idx_prev"))
    da = (aum - aum_p) if (aum and aum_p) else 0.0
    ic = ((idx / idx_p - 1) * 100) if (idx and idx_p) else 0.0
    share = (nf / da * 100) if da else 0.0
    nf_s = _s(nf, 1, True)
    if code == "price_div":
        t = ("单日净流入 <b>%s 亿</b>（250 日分位 <b>%s%%</b>，95%% 阈值 %s 亿）"
             "而上证 <b>%s%%</b>（%s）—— 资金与价格方向相反，属<b>逆势申购</b>，"
             "不是行情推动。同日篮子规模 %s 亿"
             % (nf_s, _s(i.get("pct"), 1), _s(i.get("thr"), 1), _s(ic, 2, True),
                _s(idx, 2), _s(da, 1, True)))
        if da and abs(nf) < abs(da):
            t += "，其中 <b>%s%%</b> 可由净流入解释。" % _s(share, 1)
        else:
            t += "，规模增量小于净流入（价格效应 %s 亿）。" % _s(da - nf, 1, True)
        return t
    if code == "offset":
        return ("净流入 <b>%s 亿</b> 与规模变化 <b>%s 亿</b> 几乎相抵（价格/净值效应 %s 亿）—— "
                "这是「<b>假稳</b>」：分子与分母互相抵消，单看强度读数看不出真实方向。"
                % (nf_s, _s(da, 1, True), _s(da - nf, 1, True)))
    if code == "aum_div":
        return ("净流入 <b>%s 亿</b> 而篮子规模同期 <b>%s 亿</b>（价格/净值效应 %s 亿）—— "
                "资金在进、规模在退，两个方向相反，强度读数被分母压缩。"
                % (nf_s, _s(da, 1, True), _s(da - nf, 1, True)))
    if code == "streak_div":
        return ("连续 <b>%d</b> 个确认日净流入合计 <b>%s 亿</b>（当日 <b>%s 亿</b>、分位 <b>%s%%</b>），"
                "同期上证累计 <b>%s%%</b> —— <b>持续逆势申购</b>，与「资金推动上涨」的叙事相反。"
                % (i.get("streak") or 0, _s(i.get("streak_sum"), 1, True), nf_s,
                   _s(i.get("pct"), 1), _s(i.get("streak_idx") or 0, 2, True)))
    if code == "style_div":
        return ("全宽基 <b>%s 亿</b> 而沪深300 八只 <b>%s 亿</b> —— 总量方向掩盖了结构分化，"
                "钱在篮子里换风格，不是整体进退。"
                % (nf_s, _s(i.get("nf300"), 1, True)))
    if code == "none":
        return ("净流入 <b>%s 亿</b>、上证 <b>%s%%</b>、规模 <b>%s 亿</b> 三者同向，"
                "本期无显著背离（判定阈值：显著 ≥ %s 亿）。"
                % (nf_s, _s(ic, 2, True), _s(da, 1, True),
                   _s(max(abs(i.get("thr") or 0) * ANOM_THR_RATIO, ANOM_MIN_ABS), 0)))
    return ""


def make_anomaly(k, rows):
    i = anom_inputs(k, rows)
    code = anom_code(i)
    return {"code": code, "text": _anom_text(code, i), "inputs": i}


def penetration(amv, aum):
    """篮子规模 ÷ 活跃市值（0AMV）——「资金池占比」，不是 ETF 穿透率。

    严格穿透率的分母必须是「指数自由流通市值」，当前数据不具备；
    此处用 0AMV 作分母，优点是每天可得、缺点见页面口径核对卡。
    """
    if not amv or not amv.get("v") or not aum:
        return None
    r = aum / amv["v"] * 100
    return {"pct": r, "lo": r / 1.1, "hi": r / 0.9,
            "v": amv["v"], "d": amv.get("d", ""), "src": amv.get("src", "")}



def make_verdict(payload, state, amv=None):
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

    # 本期反常：纯由当期数据生成；同一条规则既写进 data.json 也写进静态块，
    # 前端只渲染、不重写文案，避免「页面显示与数据不一致」。
    anom = make_anomaly(k, rows)
    # 活跃市值（0AMV）当日值 + 资金池占比；取不到则整体为 None（页面降级显示）。
    if amv is None:
        amv = fetch_amv()
    pen = penetration(amv, aum)

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
        "anomaly": anom["text"],
        "anomaly_code": anom["code"],
        "anomaly_inputs": anom["inputs"],
        "amv": amv,
        "pen": pen,
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
    anom = ('<div class="vanom"><b>本期反常</b>：%s</div>' % v["anomaly"]
            if v.get("anomaly") else "")
    return (
        '<section class="verdict %s" id="verdict">\n'
        '  <div class="vhead"><div class="vlab">研判结论 <span class="vtag">已确认 · 份额口径 T+1</span></div>\n'
        '    <div class="vdate">份额数据日 <b>%s</b> · 文件生成 %s</div></div>\n'
        '  <div class="vmain"><div class="vrow"><div class="vtitle">%s<span>%s</span></div>\n'
        '    <div class="vamv" id="vAmv">活跃市值 <b>--</b></div></div>\n'
        '    <div class="vdesc">%s</div>\n'
        '    %s</div>\n'
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
           v.get("title", "--"), v.get("lead", ""), v.get("desc", ""), anom,
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
