#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
东方日升 (300118, 创业板) 每日复盘生成器
- 盘后（默认 16:30 北京时间后）对个股做一份技术 + 资金复盘
- 实时/收盘行情来自东方财富 push2（个股 get）
- 日K（前复权）来自东方财富 push2his（stock/kline/get），用于计算 MA5/10/20/60 与区间涨跌
- 主力资金流向来自东方财富 push2（stock/fflow/daykline/get）
- 生成一份自包含的 HTML 复盘报告
- 运行模式：
    1) 定时模式（默认）：内部循环，每天 16:30 自动执行
    2) 单次模式：python daily_review_300118.py once  立即生成一份复盘

依赖：仅标准库 + requests（pip install requests）
"""

import os
import sys
import json
import time
import datetime
import smtplib
import email.utils
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    import requests
except ImportError:
    print("缺少依赖 requests，请执行: pip install requests")
    sys.exit(1)


# ===================== 配置区 =====================

STOCK_NAME = "东方日升"
STOCK_CODE = "300118"
SECID = "0.300118"  # 0 = 深圳，300118 = 东方日升（创业板）

# 东方财富接口
EM_GET_API = "https://push2.eastmoney.com/api/qt/stock/get"
EM_KLINE_API = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_FFLOW_API = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"

EM_HEADERS = {
    "Referer": "https://quote.eastmoney.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
REVIEW_FILE = os.path.join(OUTPUT_DIR, "daily_review_300118.html")

# 抓取重试
MAX_RETRY = 3
RETRY_INTERVAL = 5

# 定时执行时间（24小时制，北京时间）
SCHEDULE_HOUR = 16
SCHEDULE_MINUTE = 30

# 可选：生成后自动发送邮件（生产中交由 GitHub Actions 的 SMTP 步骤发送）
EMAIL_CONFIG = {
    "enable": False,
    "smtp_host": "smtp.qq.com",
    "smtp_port": 465,
    "sender": "",
    "password": "",
    "receivers": [],
}

# ===================== 基本面基线（真实数据，来自妙想，季度更新） =====================
# 以下为东方日升最新一期报告期（2026Q1，单季度）的核心财务，以及机构一致预期。
# 脚本运行时再用行情价/市值计算估值（PB、PS），形成动态研判。
FUNDAMENTALS = {
    "report_period": "2026年一季报（单季度）",
    "revenue_q": 27.02,        # 单季营业总收入（亿元）
    "revenue_yoy": -9.648,     # 营收同比 %
    "net_profit": -3.61,       # 单季归母净利润（亿元，亏损）
    "net_profit_yoy": -35.31,  # 归母净利同比 %
    "gross_margin": 2.022,     # 销售毛利率 %
    "roe": -4.27,              # 净资产收益率(加权) %
    "debt_ratio": 71.24,       # 资产负债率 %
    "bps": 7.276,              # 每股净资产（元）
    "ocf": 0.1330,             # 经营活动现金流量净额（亿元，微正）
    "rd_ratio": 2.28,          # 研发投入占营业收入 %
    "rd_staff_ratio": 17.66,   # 研发人员占比 %
}

# 机构一致预期（截至 2026-08-23，妙想）
FORECAST = [
    {"year": "2026E", "np": -5.18, "growth": 82.52},   # 仍亏损（同比减亏）
    {"year": "2027E", "np": 7.43,  "growth": 269.5},   # 扭亏为盈
    {"year": "2028E", "np": 14.43, "growth": 106.0},   # 高增
]

INDUSTRY_PROFILE = {
    "industry": "光伏设备 / 太阳能电池组件制造（申万：光伏设备）",
    "role": "全球光伏组件出货第一梯队（二线龙头），HJT 异质结技术路线先行者",
    "business": "太阳能电池组件、光伏电站 EPC、储能系统；海外营收占比较高（欧美/亚太）",
    "tech_route": "押注 N 型 HJT 异质结路线，区别于行业主流 TOPCon",
}

# 政策与行业景气框架（静态知识库，研判时引用；实时政策请以公告/新闻为准）
POLICY_FRAMEWORK = [
    ("长期需求支撑", "国内双碳目标、风光大基地建设、全球能源转型加速，光伏长期装机需求向上",
     "bull"),
    ("电价市场化（136号文）", "2025 年起新能源全面参与电力市场交易，电价波动加大、消纳与收益率承压",
     "bear"),
    ("产能出清 / 反内卷", "政策推动落后光伏产能退出、兼并重组，利好龙头但短期行业仍处价格战底部",
     "mixed"),
    ("海外贸易壁垒", "美国 UFLPA 扣留、欧盟对华光伏反补贴税、印度关税等，压制出口与盈利",
     "bear"),
    ("电网与储能配套", "源网荷储一体化、配储需求提升，有利于具备储能布局的组件企业",
     "bull"),
]


# ===================== 数据抓取 =====================

_em_session = requests.Session()


def _get_json(url, params, timeout=12):
    last_err = None
    for _ in range(MAX_RETRY):
        try:
            resp = _em_session.get(url, params=params, headers=EM_HEADERS, timeout=timeout)
            return resp.json()
        except Exception as e:  # noqa
            last_err = e
            time.sleep(RETRY_INTERVAL)
    print(f"[WARN] 请求失败 {url}: {last_err}")
    return None


def fetch_quote():
    """个股实时/收盘行情。返回 dict；失败返回 None。"""
    fields = ("f12,f13,f14,f43,f44,f45,f46,f47,f48,f57,f58,f60,"
              "f116,f117,f162,f167,f168,f169,f171,f172")
    d = _get_json(EM_GET_API, {
        "secid": SECID,
        "fields": fields,
        "invt": 2,
        "fltt": 2,
    })
    if not d or "data" not in d or not d["data"]:
        print("[WARN] 个股行情抓取失败")
        return None
    x = d["data"]
    price = x.get("f43")
    prev_close = x.get("f60")
    if not isinstance(price, (int, float)) or not isinstance(prev_close, (int, float)) or prev_close == 0:
        return None
    return {
        "name": x.get("f58") or STOCK_NAME,
        "code": x.get("f57") or STOCK_CODE,
        "price": float(price),
        "open": x.get("f46"),
        "high": x.get("f44"),
        "low": x.get("f45"),
        "prev_close": float(prev_close),
        "volume": x.get("f47"),          # 手
        "amount": x.get("f48"),          # 元
        "turnover": x.get("f162"),       # 换手率 %
        "amplitude": x.get("f167"),      # 振幅 %
        "pe_ttm": x.get("f168"),         # 市盈率TTM
        "vol_ratio": x.get("f169"),      # 量比
        "limit_up": x.get("f171"),       # 涨停价
        "limit_down": x.get("f172"),     # 跌停价
        "total_mv": x.get("f116"),       # 总市值 元
        "float_mv": x.get("f117"),       # 流通市值 元
    }


def fetch_kline(datalen=120):
    """个股日K（前复权），返回 list[dict]（升序）。失败返回 None。"""
    d = _get_json(EM_KLINE_API, {
        "secid": SECID,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101,     # 日K
        "fqt": 1,       # 前复权
        "end": "20500101",
        "lmt": datalen,
    })
    k = d.get("data") if d else None
    if not k or not k.get("klines"):
        print("[WARN] 个股日K抓取失败")
        return None
    out = []
    for line in k["klines"]:
        p = line.split(",")
        if len(p) < 7:
            continue
        try:
            out.append({
                "date": p[0],
                "open": float(p[1]),
                "close": float(p[2]),
                "high": float(p[3]),
                "low": float(p[4]),
                "volume": float(p[5]),   # 手
                "amount": float(p[6]),    # 元
            })
        except ValueError:
            continue
    return out if out else None


def fetch_fund_flow(days=10):
    """主力资金流向（日级）。返回 list[dict]（升序）。
    字段布局：f51日期, f52主力净流入(元), f53超大单, f54大单, f55中单, f56小单。失败返回 []。"""
    d = _get_json(EM_FFLOW_API, {
        "lmt": days,
        "klt": 101,
        "secid": SECID,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56",
    })
    k = d.get("data") if d else None
    if not k or not k.get("klines"):
        print("[WARN] 资金流向抓取失败")
        return []
    out = []
    for line in k["klines"]:
        p = line.split(",")
        if len(p) < 6:
            continue
        try:
            out.append({
                "date": p[0],
                "main_net": float(p[1]),    # 主力净流入额 元
                "huge_net": float(p[2]),    # 超大单净流入 元
                "big_net": float(p[3]),     # 大单净流入 元
                "mid_net": float(p[4]),     # 中单净流入 元
                "small_net": float(p[5]),   # 小单净流入 元
            })
        except ValueError:
            continue
    return out


# ===================== 指标计算 =====================

def calc_ma(vals, n):
    if vals and len(vals) >= n:
        return sum(vals[-n:]) / n
    return None


def calc_n_chg(closes, n):
    """近 n 日涨跌幅（%）：最新收盘 vs n 个交易日前收盘。"""
    if closes and len(closes) > n and closes[-1 - n]:
        return (closes[-1] / closes[-1 - n] - 1) * 100
    return None


def ma_arrangement(price, ma5, ma10, ma20, ma60=None):
    """均线排列：多头(价≥MA5≥MA10≥MA20[≥MA60]) / 空头 / 纠结 / 数据不足。"""
    seq = [price, ma5, ma10, ma20]
    if ma60 is not None:
        seq.append(ma60)
    if not all(isinstance(v, float) for v in seq):
        return ("数据不足", "neutral")
    if all(seq[i] >= seq[i + 1] for i in range(len(seq) - 1)):
        return ("多头排列", "bull")
    if all(seq[i] <= seq[i + 1] for i in range(len(seq) - 1)):
        return ("空头排列", "bear")
    return ("均线纠结", "mixed")


# ===================== 格式化辅助 =====================

def fmt(v, digits=2):
    if isinstance(v, float):
        return f"{v:,.{digits}f}"
    return str(v)


def fmt_yi(v):
    """元 -> 亿元，保留两位小数。"""
    if isinstance(v, (int, float)):
        return f"{v / 1e8:,.2f}亿"
    return "—"


def color_style(up):
    if up is True:
        return "#e23b3b"    # 涨：红
    if up is False:
        return "#16a34a"   # 跌：绿
    return "#888888"


def arrow(up):
    if up is True:
        return "▲"
    if up is False:
        return "▼"
    return "—"


def ma_position_color(price, ma):
    if isinstance(price, float) and isinstance(ma, float):
        return "#e23b3b" if price >= ma else "#16a34a"
    return "#888888"


BADGE = {
    "bull":   ("多头排列", "#e23b3b", "#fff"),
    "bear":   ("空头排列", "#16a34a", "#fff"),
    "mixed":  ("均线纠结", "#f59e0b", "#fff"),
    "neutral":("数据不足", "#9ca3af", "#fff"),
}
CARD_EDGE = {"bull": "#e23b3b", "bear": "#16a34a", "mixed": "#f59e0b", "neutral": "#eef2f7"}


# ===================== 复盘数据组装 =====================

def build_review():
    q = fetch_quote()
    kl = fetch_kline(120)
    ff = fetch_fund_flow(10)

    if not q:
        # 行情兜底：用日K最后一根
        if kl:
            last = kl[-1]
            q = {
                "name": STOCK_NAME, "code": STOCK_CODE,
                "price": last["close"], "open": last["open"],
                "high": last["high"], "low": last["low"],
                "prev_close": kl[-2]["close"] if len(kl) > 1 else last["open"],
                "volume": last["volume"], "amount": last["amount"],
                "turnover": None, "amplitude": None, "pe_ttm": None,
                "vol_ratio": None, "limit_up": None, "limit_down": None,
                "total_mv": None, "float_mv": None,
            }
        else:
            return None

    closes = [x["close"] for x in kl] if kl else []
    volumes = [x["volume"] for x in kl] if kl else []

    price = q["price"]
    change = price - q["prev_close"]
    pct = (change / q["prev_close"] * 100) if q["prev_close"] else 0.0
    up = change > 0

    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    ma60 = calc_ma(closes, 60)

    vol5 = calc_ma(volumes, 5)
    vol10 = calc_ma(volumes, 10)
    vol_ratio_hist = (volumes[-1] / vol5) if (volumes and vol5) else None

    # 近20日 高/低 作为 压力/支撑
    recent = closes[-20:] if len(closes) >= 20 else closes
    support = min(recent) if recent else None
    pressure = max(recent) if recent else None

    arr, arr_cls = ma_arrangement(price, ma5, ma10, ma20, ma60)

    today_ff = ff[-1] if ff else None

    return {
        "q": q, "price": price, "change": change, "pct": pct, "up": up,
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "vol5": vol5, "vol10": vol10, "vol_ratio_hist": vol_ratio_hist,
        "support": support, "pressure": pressure,
        "arr": arr, "arr_cls": arr_cls,
        "chg5": calc_n_chg(closes, 5),
        "chg10": calc_n_chg(closes, 10),
        "chg20": calc_n_chg(closes, 20),
        "chg60": calc_n_chg(closes, 60),
        "fund_flow": ff,
        "today_ff": today_ff,
        "klines": kl,
    }


# ===================== 资金面研判 =====================

def analyze_fund_flow(r):
    """对各类交易资金做综合研判，返回 dict；数据不足返回 None。"""
    ff = r.get("fund_flow") or []
    today = r.get("today_ff")
    if not ff or not today or not isinstance(today.get("main_net"), (int, float)):
        return None

    q = r["q"]
    amount = q.get("amount")  # 元
    main_net = today["main_net"]
    huge = today.get("huge_net") or 0.0
    big = today.get("big_net") or 0.0
    mid = today.get("mid_net") or 0.0
    small = today.get("small_net") or 0.0
    main_in = main_net >= 0

    # 主力净流入占今日成交额比，用于判断强弱
    main_ratio = (main_net / amount * 100) if isinstance(amount, (int, float)) and amount else None
    if isinstance(main_ratio, float):
        ar = abs(main_ratio)
        strength = "大幅" if ar >= 5 else "明显" if ar >= 2 else "小幅" if ar >= 0.5 else "微弱"
    else:
        strength = ""

    # 资金博弈：主力 vs 散户（中单+小单）
    retail_net = mid + small
    if main_in and retail_net <= 0:
        battle = ("主力吸筹", "机构在买入、散户在卖出，筹码向主力集中", "bull")
    elif (not main_in) and retail_net >= 0:
        battle = ("主力派发", "机构在卖出、散户在接盘，警惕高位派发", "bear")
    elif main_in and retail_net > 0:
        battle = ("资金全面流入", "主力与散户共振做多，做多情绪一致", "bull")
    else:
        battle = ("资金全面流出", "主力与散户共振离场，抛压较重", "bear")

    # 超大单 vs 大单 结构
    if huge >= 0 and big >= 0:
        hb = ("超大单+大单共振流入", "机构与大户一致做多", "bull")
    elif huge >= 0 and big < 0:
        hb = ("超大单流入、大单流出", "机构建仓但大户有分歧", "mixed")
    elif huge < 0 and big >= 0:
        hb = ("超大单流出、大单流入", "机构撤退、游资/大户拉抬", "mixed")
    else:
        hb = ("超大单+大单共振流出", "机构与大户一致出逃", "bear")

    # 近几日主力趋势（按可用天数）
    n = len(ff)
    s3 = sum(x["main_net"] for x in ff[-3:]) if n else 0.0
    s5 = sum(x["main_net"] for x in ff[-5:]) if n else 0.0

    def trend_label(s, days):
        if s > 0:
            return (f"近{days}日主力净流入", "bull")
        if s < 0:
            return (f"近{days}日主力净流出", "bear")
        return (f"近{days}日主力进出均衡", "neutral")

    t3 = trend_label(s3, min(n, 3))
    t5 = trend_label(s5, min(n, 5)) if n > 3 else t3

    # 价量配合
    up = r["up"]
    if up and main_in:
        pv = ("价涨量增、主力流入", "量价配合，上攻有资金支撑", "bull")
    elif (not up) and main_in:
        pv = ("价跌主力流入", "下跌中主力低吸，关注企稳信号", "mixed")
    elif up and (not main_in):
        pv = ("价涨主力流出", "上涨缺乏主力支撑，警惕拉高出货", "bear")
    else:
        pv = ("价跌主力流出", "资金出逃与下跌共振，趋势偏弱", "bear")

    # 综合信号评分
    score = 0
    score += 1 if main_in else -1
    score += 1 if hb[2] == "bull" else (-1 if hb[2] == "bear" else 0)
    score += 1 if t3[1] == "bull" else (-1 if t3[1] == "bear" else 0)
    score += 1 if pv[2] == "bull" else (-1 if pv[2] == "bear" else 0)
    if score >= 2:
        signal = ("资金面偏多", "bull")
    elif score <= -2:
        signal = ("资金面偏空", "bear")
    else:
        signal = ("资金面中性/分歧", "mixed")

    return {
        "main_net": main_net, "main_ratio": main_ratio, "strength": strength,
        "battle": battle, "hb": hb, "t3": t3, "t5": t5, "pv": pv,
        "signal": signal, "huge": huge, "big": big, "mid": mid,
        "small": small, "n_days": n,
    }


# ===================== 基本面 & 政策面研判 =====================

def analyze_fundamentals(r):
    """结合运行时行情与基线财务，做基本面（估值/盈利/成长/偿债）研判。"""
    q = r["q"]
    price = r["price"]
    total_mv = q.get("total_mv")     # 元
    bps = FUNDAMENTALS["bps"]

    # 估值（动态）
    pb = price / bps if bps else None
    annual_rev = FUNDAMENTALS["revenue_q"] * 4   # 年化营收（亿元）
    ps = ((total_mv / 1e8) / annual_rev) if (isinstance(total_mv, (int, float)) and total_mv and annual_rev) else None
    pe_ttm = q.get("pe_ttm")

    # 估值水位判断
    if isinstance(pb, float):
        if pb < 1.0:
            pb_judge = ("破净", "股价低于每股净资产，估值处于历史极低分位", "bull")
        elif pb < 1.5:
            pb_judge = ("历史低位", "PB 贴近净资产，安全边际较高但需警惕价值陷阱", "bull")
        elif pb < 2.5:
            pb_judge = ("中性区间", "PB 处于行业中枢附近", "mixed")
        else:
            pb_judge = ("偏高", "PB 高于行业中枢，需盈利兑现支撑", "bear")
    else:
        pb_judge = ("数据不足", "", "neutral")

    if isinstance(ps, float):
        if ps < 0.6:
            ps_judge = ("偏低", "市销率低于 0.6，隐含市场对营收含金量悲观", "bull")
        elif ps < 1.3:
            ps_judge = ("中性", "市销率处于制造板块中枢", "mixed")
        else:
            ps_judge = ("偏高", "市销率高于 1.3，需高成长验证", "bear")
    else:
        ps_judge = ("数据不足", "", "neutral")

    # 盈利研判
    gm = FUNDAMENTALS["gross_margin"]
    roe = FUNDAMENTALS["roe"]
    np_ = FUNDAMENTALS["net_profit"]
    if np_ < 0 and gm < 10 and roe < 0:
        profit_judge = ("盈利承压", f"最新报告期亏损（归母 {np_}亿）、毛利率仅 {gm}%、ROE {roe}%，制造端价格战致盈利触底", "bear")
    elif np_ >= 0 and roe >= 8:
        profit_judge = ("盈利稳健", "最新报告期盈利与 ROE 健康", "bull")
    else:
        profit_judge = ("盈利偏弱", "盈利能力处于修复途中", "mixed")

    # 成长研判（结合机构预期）
    rev_yoy = FUNDAMENTALS["revenue_yoy"]
    f27 = next((x for x in FORECAST if x["year"] == "2027E"), None)
    if rev_yoy < 0 and f27 and f27["np"] > 0:
        growth_judge = ("短期下滑、困境反转预期", f"营收同比 {rev_yoy}%，但机构一致预期 {f27['year']} 扭亏至 {f27['np']}亿，隐含行业出清后修复", "mixed")
    elif rev_yoy >= 0:
        growth_judge = ("营收增长", f"营收同比 {rev_yoy}%，成长延续", "bull")
    else:
        growth_judge = ("营收下滑", f"营收同比 {rev_yoy}%，需求或价格承压", "bear")

    # 偿债 / 现金流研判
    dr = FUNDAMENTALS["debt_ratio"]
    ocf = FUNDAMENTALS["ocf"]
    if dr >= 70:
        debt_judge = ("杠杆偏高", f"资产负债率 {dr}%，重资产+HJT 产线投入大，财务弹性有限", "bear")
    elif dr >= 50:
        debt_judge = ("杠杆适中", f"资产负债率 {dr}%，处于制造业中上水平", "mixed")
    else:
        debt_judge = ("杠杆偏低", f"资产负债率 {dr}%，财务结构稳健", "bull")
    if ocf and ocf > 0:
        cash_note = f"经营现金流 {ocf}亿（微正），对高负债的覆盖偏弱"
    else:
        cash_note = "经营现金流承压"

    # 综合基本面信号打分
    score = 0
    score += 1 if pb_judge[2] == "bull" else (-1 if pb_judge[2] == "bear" else 0)
    score += 1 if ps_judge[2] == "bull" else (-1 if ps_judge[2] == "bear" else 0)
    score += 1 if profit_judge[2] == "bull" else (-1 if profit_judge[2] == "bear" else 0)
    score += 1 if growth_judge[2] == "bull" else (-1 if growth_judge[2] == "bear" else 0)
    score += 1 if debt_judge[2] == "bull" else (-1 if debt_judge[2] == "bear" else 0)
    if score >= 2:
        signal = ("基本面偏多", "bull")
    elif score <= -2:
        signal = ("基本面偏空", "bear")
    else:
        signal = ("基本面中性/筑底", "mixed")

    return {
        "pb": pb, "ps": ps, "pe_ttm": pe_ttm,
        "pb_judge": pb_judge, "ps_judge": ps_judge,
        "profit_judge": profit_judge, "growth_judge": growth_judge,
        "debt_judge": debt_judge, "cash_note": cash_note,
        "signal": signal, "annual_rev": annual_rev,
    }


def analyze_policy():
    """行业与政策面研判（基于静态框架 + 基线财务）。"""
    # 行业景气结论
    industry_tone = ("周期底部、出清进行中", "光伏制造产能过剩、组件价格跌破成本线，全行业盈利触底；落后产能出清后格局优化", "mixed")
    # 政策综合结论：长期友好 + 短期贸易壁垒/电价市场化压制
    policy_tone = ("长期友好、短期承压", "能源转型方向不变支撑长期需求；但海外贸易壁垒与电价市场化短期压制出口与收益率", "mixed")
    # 公司相关（结合高负债 + HJT + 海外）
    company_note = ("高负债 + HJT 重资产 + 高海外占比", "对组件价格、融资成本与贸易政策高度敏感；HJT 若率先放量可形成差异化，否则面临 TOPCon 主流挤压", "mixed")
    return {
        "industry_tone": industry_tone,
        "policy_tone": policy_tone,
        "company_note": company_note,
        "points": POLICY_FRAMEWORK,
    }


# ===================== HTML 生成 =====================

def render_fund_flow_bars(ff):
    if not ff:
        return '<div class="muted">资金流向数据暂不可用</div>'
    rows = []
    for item in ff:  # 升序
        net = item["main_net"]
        if not isinstance(net, (int, float)):
            continue
        sign = net >= 0
        bar_c = "#e23b3b" if sign else "#16a34a"
        rows.append((item["date"], net, sign, bar_c))
    if not rows:
        return '<div class="muted">资金流向数据暂不可用</div>'
    max_abs = max(abs(r[1]) for r in rows) or 1
    html = '<div class="ff-list">'
    for date, net, sign, bar_c in reversed(rows):
        w = abs(net) / max_abs * 100
        label = "流入" if sign else "流出"
        bar = (f'<div class="ff-bar-wrap"><div class="ff-bar" '
               f'style="width:{w:.1f}%;background:{bar_c}"></div></div>')
        html += (f'<div class="ff-row"><span class="ff-date">{date[5:]}</span>'
                 f'{bar}'
                 f'<span class="ff-val" style="color:{bar_c}">'
                 f'{label}{fmt_yi(abs(net))}</span></div>')
    html += '</div>'
    return html


def render_fund_analysis(fa):
    if not fa:
        return '<div class="note">资金面研判：数据不足</div>'
    sig_label, sig_cls = fa["signal"]
    s_badge = BADGE.get(sig_cls, BADGE["neutral"])

    rows = [
        ("主力资金", fa["main_net"]),
        ("超大单", fa["huge"]),
        ("大单", fa["big"]),
        ("中单", fa["mid"]),
        ("小单(散户)", fa["small"]),
    ]
    tb = ('<table class="ff-table"><tr><th>类别</th><th>净流入(亿)</th>'
          '<th>方向</th></tr>')
    for name, val in rows:
        if isinstance(val, float):
            c = "#e23b3b" if val >= 0 else "#16a34a"
            tb += (f'<tr><td>{name}</td>'
                   f'<td style="color:{c}">{val / 1e8:+.2f}</td>'
                   f'<td style="color:{c}">{"流入" if val >= 0 else "流出"}</td></tr>')
        else:
            tb += f'<tr><td>{name}</td><td colspan="2" style="color:#888">—</td></tr>'
    tb += '</table>'

    mr = fa["main_ratio"]
    if isinstance(mr, float):
        mrc = "#e23b3b" if mr >= 0 else "#16a34a"
        mr_txt = (f'主力净流入占今日成交额 <b style="color:{mrc}">{mr:+.2f}%</b>'
                  f'（{fa["strength"]}）')
    else:
        mr_txt = '主力净流入占比：数据不足'

    cls_color = {"bull": "#e23b3b", "bear": "#16a34a",
                 "mixed": "#d97706", "neutral": "#6b7280"}
    bullets = [
        ("资金博弈", fa["battle"]),
        ("大单结构", fa["hb"]),
        ("近期趋势", (fa["t3"][0] + (f'；{fa["t5"][0]}' if fa["n_days"] > 3 else ""),
                      "观察主力资金连续性", fa["t3"][1])),
        ("价量配合", fa["pv"]),
    ]
    bl = '<ul class="ff-bullets">'
    for title, (txt, desc, cls) in bullets:
        col = cls_color.get(cls, "#6b7280")
        bl += (f'<li><b style="color:{col}">{title}：{txt}</b>'
               f' — <span style="color:#4b5563">{desc}</span></li>')
    bl += '</ul>'

    return f"""
    <div class="card" style="border-color:{CARD_EDGE.get(sig_cls, '#eef2f7')}">
      <div class="badge-row">
        <span class="b-name">资金面研判</span>
        <span class="badge" style="background:{s_badge[1]};color:{s_badge[2]}">{sig_label}</span>
      </div>
      {tb}
      <div class="note" style="margin-top:10px">{mr_txt}</div>
      {bl}
    </div>"""


def render_fundamental(fa, pa, r):
    """渲染基本面研判 + 行业政策面两张卡片。"""
    cls_color = {"bull": "#e23b3b", "bear": "#16a34a",
                 "mixed": "#d97706", "neutral": "#6b7280"}
    sig_label, sig_cls = fa["signal"]
    s_badge = BADGE.get(sig_cls, BADGE["neutral"])
    edge = CARD_EDGE.get(sig_cls, "#eef2f7")

    prof = INDUSTRY_PROFILE
    prof_html = (f'<div class="note" style="background:#f0f9ff;border-color:#bae6fd;color:#0c4a6e">'
                f'<b>行业：</b>{prof["industry"]}<br>'
                f'<b>定位：</b>{prof["role"]}<br>'
                f'<b>主营：</b>{prof["business"]}<br>'
                f'<b>技术：</b>{prof["tech_route"]}</div>')

    fm = FUNDAMENTALS
    fin_rows = [
        ("报告期", fm["report_period"], ""),
        ("单季营收", f'{fmt(fm["revenue_q"])}亿（同比 {fm["revenue_yoy"]:+.2f}%）',
         "bear" if fm["revenue_yoy"] < 0 else "bull"),
        ("单季归母净利润", f'{fmt(fm["net_profit"])}亿（同比 {fm["net_profit_yoy"]:+.2f}%）',
         "bear" if fm["net_profit"] < 0 else "bull"),
        ("销售毛利率", f'{fmt(fm["gross_margin"])}%',
         "bear" if fm["gross_margin"] < 10 else "bull"),
        ("ROE(加权)", f'{fmt(fm["roe"])}%',
         "bear" if fm["roe"] < 0 else "bull"),
        ("资产负债率", f'{fmt(fm["debt_ratio"])}%',
         "bear" if fm["debt_ratio"] >= 70 else "mixed"),
        ("每股净资产", f'{fmt(fm["bps"])}元', ""),
        ("经营现金流净额", f'{fmt(fm["ocf"])}亿', "mixed"),
        ("研发投入占营收", f'{fmt(fm["rd_ratio"])}%', "mixed"),
    ]
    ftb = '<table class="ff-table"><tr><th>指标</th><th>数值</th></tr>'
    for k, v, cl in fin_rows:
        col = cls_color.get(cl, "#374151") if cl else "#374151"
        ftb += f'<tr><td>{k}</td><td style="color:{col}">{v}</td></tr>'
    ftb += '</table>'

    ftb2 = '<table class="ff-table"><tr><th>年度</th><th>预测归母净利(亿)</th><th>增速</th></tr>'
    for x in FORECAST:
        cl = "bull" if x["np"] > 0 else "bear"
        col = cls_color.get(cl, "#374151")
        ftb2 += (f'<tr><td>{x["year"]}</td>'
                 f'<td style="color:{col}">{x["np"]:+.2f}</td>'
                 f'<td>{x["growth"]:+.1f}%</td></tr>')
    ftb2 += '</table>'

    pb = fa["pb"]; ps = fa["ps"]; pe = fa["pe_ttm"]
    pb_txt = f'{pb:.2f}（{fa["pb_judge"][0]}）' if isinstance(pb, float) else "—"
    ps_txt = f'{ps:.2f}（{fa["ps_judge"][0]}）' if isinstance(ps, float) else "—"
    if isinstance(pe, float) and pe < 0:
        pe_txt = f'{fmt(pe)}（亏损股 PE 参考意义有限）'
    elif isinstance(pe, float):
        pe_txt = fmt(pe)
    else:
        pe_txt = "—"
    val_html = (f'<div class="note" style="margin-top:10px">'
                f'动态估值（基于现价 {fmt(r["price"])} 与市值）：'
                f'PB <b>{pb_txt}</b>；PS <b>{ps_txt}</b>；PE(TTM) <b>{pe_txt}</b>。'
                f'年化营收约 {fmt(fa["annual_rev"])}亿。')
    if fm["net_profit"] < 0:
        val_html += (' <span style="color:#b45309">'
                     '（最新单季仍亏损，PE(TTM) 为滚动口径，盈利修复前参考有限）</span>')
    val_html += '</div>'

    bullets = [
        ("估值面", fa["pb_judge"]),
        ("盈利面", fa["profit_judge"]),
        ("成长面", fa["growth_judge"]),
        ("偿债/现金流", (fa["debt_judge"][0],
                         fa["debt_judge"][1] + "；" + fa["cash_note"],
                         fa["debt_judge"][2])),
    ]
    bl = '<ul class="ff-bullets">'
    for title, (txt, desc, cl) in bullets:
        col = cls_color.get(cl, "#6b7280")
        bl += (f'<li><b style="color:{col}">{title}：{txt}</b>'
               f' — <span style="color:#4b5563">{desc}</span></li>')
    bl += '</ul>'

    fund_card = f"""
    <div class="card" style="border-color:{edge}">
      <div class="badge-row">
        <span class="b-name">基本面研判</span>
        <span class="badge" style="background:{s_badge[1]};color:{s_badge[2]}">{sig_label}</span>
      </div>
      {prof_html}
      <div style="margin-top:10px;font-size:13px;color:#374151">核心财务（{fm['report_period']}，来源：妙想/东财）</div>
      {ftb}
      <div style="margin-top:10px;font-size:13px;color:#374151">机构一致预期（截至 2026-08-23）</div>
      {ftb2}
      {val_html}
      {bl}
    </div>"""

    ppoints = pa["points"]
    ptb = '<table class="ff-table"><tr><th>要点</th><th>说明</th><th>方向</th></tr>'
    for t, desc, cl in ppoints:
        col = cls_color.get(cl, "#374151")
        lab = {"bull": "偏多", "bear": "偏空", "mixed": "中性"}.get(cl, "")
        ptb += f'<tr><td>{t}</td><td>{desc}</td><td style="color:{col}">{lab}</td></tr>'
    ptb += '</table>'

    pol_bullets = [
        ("行业景气", pa["industry_tone"]),
        ("政策综合", pa["policy_tone"]),
        ("公司相关", pa["company_note"]),
    ]
    pbl = '<ul class="ff-bullets">'
    for title, (txt, desc, cl) in pol_bullets:
        col = cls_color.get(cl, "#6b7280")
        pbl += (f'<li><b style="color:{col}">{title}：{txt}</b>'
                f' — <span style="color:#4b5563">{desc}</span></li>')
    pbl += '</ul>'

    pol_card = f"""
    <div class="card" style="border-color:#fdba74">
      <div class="badge-row">
        <span class="b-name">行业与政策面</span>
        <span class="badge" style="background:#d97706;color:#fff">长期友好/短期承压</span>
      </div>
      {ptb}
      {pbl}
      <div class="note" style="margin-top:10px;background:#fff7ed;border-color:#fed7aa;color:#7c2d12">
        提示：政策与行业景气为静态框架研判，实时政策、关税与装机数据请以交易所公告及权威新闻为准。</div>
    </div>"""

    return fund_card + pol_card


def render_review(r):
    q = r["q"]
    up = r["up"]
    c = color_style(up)
    a = arrow(up)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    weekday = ["星期一", "星期二", "星期三", "星期四", "星期五",
               "星期六", "星期日"][datetime.datetime.now().weekday()]

    badge_label, badge_bg, badge_fg = BADGE.get(r["arr_cls"], BADGE["neutral"])
    edge = CARD_EDGE.get(r["arr_cls"], "#eef2f7")

    # 关键指标格
    def kv(label, value, color=None):
        v = f'<span style="color:{color}">{value}</span>' if color else value
        return f'<div class="kv"><span class="kv-k">{label}</span><span class="kv-v">{v}</span></div>'

    chg_c = c
    kv_chg = kv("涨跌幅", f"{a} {r['pct']:+.2f}%", chg_c)
    kv_amt = kv("涨跌额", f"{r['change']:+.2f}", chg_c)
    kv_close = kv("收盘价", fmt(r["price"]), c)
    kv_open = kv("今开", fmt(q.get("open")))
    kv_high = kv("最高", fmt(q.get("high")))
    kv_low = kv("最低", fmt(q.get("low")))
    kv_prev = kv("昨收", fmt(q.get("prev_close")))
    kv_turn = kv("换手率", (fmt(q.get("turnover")) + "%") if isinstance(q.get("turnover"), float) else "—")
    kv_volr = kv("量比", fmt(q.get("vol_ratio")) if isinstance(q.get("vol_ratio"), float) else "—")
    kv_amp = kv("振幅", (fmt(q.get("amplitude")) + "%") if isinstance(q.get("amplitude"), float) else "—")
    kv_amount = kv("成交额", fmt_yi(q.get("amount")))
    kv_pe = kv("市盈率TTM", fmt(q.get("pe_ttm")) if isinstance(q.get("pe_ttm"), float) else "—")
    kv_tmv = kv("总市值", fmt_yi(q.get("total_mv")))
    kv_fmv = kv("流通市值", fmt_yi(q.get("float_mv")))

    # 均线
    def ma_cell(n, ma):
        if isinstance(ma, float):
            pos_c = ma_position_color(r["price"], ma)
            pc = "站上" if r["price"] >= ma else "跌破"
            return (f'<div class="ma"><span class="ma-n">MA{n}</span>'
                    f'<span class="ma-v" style="color:{pos_c}">{fmt(ma)}</span>'
                    f'<span class="ma-tag" style="color:{pos_c}">{pc}</span></div>')
        return (f'<div class="ma"><span class="ma-n">MA{n}</span>'
                f'<span class="ma-v" style="color:#888">—</span></div>')

    ma_row = "".join(ma_cell(n, r.get(f"ma{n}")) for n in (5, 10, 20, 60))

    # 区间涨跌
    def range_cell(label, v):
        if isinstance(v, float):
            vc = "#e23b3b" if v >= 0 else "#16a34a"
            return (f'<div class="rg"><span class="rg-n">{label}</span>'
                    f'<span class="rg-v" style="color:{vc}">{v:+.2f}%</span></div>')
        return (f'<div class="rg"><span class="rg-n">{label}</span>'
                f'<span class="rg-v" style="color:#888">—</span></div>')

    range_row = "".join(range_cell(lbl, r.get(k))
                        for lbl, k in (("近5日", "chg5"), ("近10日", "chg10"),
                                       ("近20日", "chg20"), ("近60日", "chg60")))

    # 量能
    vr = r.get("vol_ratio_hist")
    if isinstance(vr, float):
        if vr >= 1.5:
            vol_label, vol_c = "明显放量", "#e23b3b"
        elif vr >= 1.0:
            vol_label, vol_c = "温和放量", "#e23b3b"
        elif vr >= 0.7:
            vol_label, vol_c = "温和缩量", "#16a34a"
        else:
            vol_label, vol_c = "明显缩量", "#16a34a"
        vol_html = (f'量能：今日成交量约为 5 日均量的 <b style="color:{vol_c}">'
                    f'{vr:.2f}倍</b>（{vol_label}），5日均量 {fmt_yi(r["vol5"]*100) if r.get("vol5") else "—"}，'
                    f'10日均量 {fmt_yi(r["vol10"]*100) if r.get("vol10") else "—"}')
    else:
        vol_html = "量能：数据不足"

    # 资金流向
    tff = r.get("today_ff")
    if tff and isinstance(tff.get("main_net"), (int, float)):
        net = tff["main_net"]
        huge = tff.get("huge_net")
        big = tff.get("big_net")
        small = tff.get("small_net")
        fc = "#e23b3b" if net >= 0 else "#16a34a"
        parts = [f'{"净流入" if net >= 0 else "净流出"} <b style="color:{fc}">{fmt_yi(abs(net))}</b>']
        if isinstance(huge, float):
            parts.append(f'超大单 {fmt_yi(huge)}')
        if isinstance(big, float):
            parts.append(f'大单 {fmt_yi(big)}')
        if isinstance(small, float):
            parts.append(f'小单 {fmt_yi(small)}')
        today_ff_html = "今日主力资金：" + "，".join(parts)
    else:
        today_ff_html = '今日主力资金流向：数据不足'

    # 支撑压力
    sp_html = (f'近20日区间：支撑位约 <b>{fmt(r["support"])}</b> ，'
               f'压力位约 <b>{fmt(r["pressure"])}</b>'
               if isinstance(r.get("support"), float) and isinstance(r.get("pressure"), float)
               else "支撑/压力：数据不足")

    ff_bars = render_fund_flow_bars(r.get("fund_flow"))
    fa = analyze_fund_flow(r)
    fa2 = analyze_fundamentals(r)
    pa2 = analyze_policy()

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{STOCK_NAME}({STOCK_CODE}) 每日复盘</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background:#f4f6fa; margin:0; padding:24px; color:#1f2937; }}
  .wrap {{ max-width:880px; margin:0 auto; background:#fff; border-radius:14px;
          overflow:hidden; box-shadow:0 6px 24px rgba(0,0,0,.08); }}
  .header {{ background:linear-gradient(135deg,#0f766e,#10b981); color:#fff; padding:28px 32px; }}
  .header h1 {{ margin:0; font-size:22px; }}
  .header .meta {{ margin-top:8px; font-size:13px; opacity:.9; }}
  .legend {{ background:#ecfdf5; color:#065f46; font-size:12px;
            padding:8px 32px; display:flex; gap:16px; flex-wrap:wrap; }}
  .legend b.bull {{ color:#e23b3b; }}
  .legend b.bear {{ color:#16a34a; }}
  .legend b.mixed {{ color:#d97706; }}
  .section {{ padding:18px 32px; }}
  .section h2 {{ font-size:16px; margin:6px 0 14px; color:#111827;
                border-left:4px solid #10b981; padding-left:10px; }}
  .top {{ display:flex; justify-content:space-between; align-items:center;
         padding:18px 32px 6px; border-top:1px solid #f3f4f6; }}
  .top .name {{ font-size:18px; font-weight:700; }}
  .top .code {{ font-size:13px; color:#6b7280; }}
  .price-big {{ font-size:34px; font-weight:800; font-variant-numeric:tabular-nums; }}
  .chg-big {{ font-size:16px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .kv-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(120px,1fr));
             gap:10px; margin-top:8px; }}
  .kv {{ background:#f8fafc; border:1px solid #eef2f7; border-radius:8px;
        padding:8px 10px; }}
  .kv-k {{ display:block; font-size:11px; color:#9ca3af; }}
  .kv-v {{ display:block; font-size:15px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .card {{ border:1px solid #eef2f7; border-left-width:4px; border-radius:12px;
          padding:14px 16px; margin-bottom:12px; background:#fafbfc; }}
  .badge-row {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; }}
  .b-name {{ font-size:15px; font-weight:700; }}
  .badge {{ font-size:11px; font-weight:700; padding:3px 10px; border-radius:999px; white-space:nowrap; }}
  .ma-row {{ display:flex; gap:10px; margin-top:12px; flex-wrap:wrap; }}
  .ma {{ flex:1; min-width:84px; background:#fff; border:1px solid #eef2f7;
        border-radius:8px; padding:8px 10px; }}
  .ma-n {{ display:block; font-size:11px; color:#9ca3af; }}
  .ma-v {{ display:block; font-size:15px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .ma-tag {{ font-size:11px; }}
  .rg-row {{ display:flex; gap:10px; margin-top:10px; flex-wrap:wrap; }}
  .rg {{ flex:1; min-width:84px; text-align:center; background:#fff;
        border:1px solid #eef2f7; border-radius:8px; padding:8px 6px; }}
  .rg-n {{ display:block; font-size:11px; color:#9ca3af; }}
  .rg-v {{ display:block; font-size:14px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .note {{ font-size:13px; color:#374151; background:#f0fdfa; border:1px solid #ccfbf1;
          border-radius:8px; padding:10px 12px; margin-top:12px; line-height:1.7; }}
  .ff-list {{ margin-top:6px; }}
  .ff-row {{ display:flex; align-items:center; gap:8px; margin:5px 0; font-size:12px; }}
  .ff-date {{ width:46px; color:#6b7280; font-variant-numeric:tabular-nums; }}
  .ff-bar-wrap {{ flex:1; background:#f1f5f9; border-radius:4px; height:14px; overflow:hidden; }}
  .ff-bar {{ height:14px; border-radius:4px; }}
  .ff-val {{ width:160px; text-align:right; font-variant-numeric:tabular-nums; }}
  .ff-table {{ width:100%; border-collapse:collapse; margin-top:6px; font-size:13px; }}
  .ff-table th {{ text-align:left; color:#9ca3af; font-weight:600; padding:4px 6px; border-bottom:1px solid #eef2f7; }}
  .ff-table td {{ padding:4px 6px; border-bottom:1px solid #f3f4f6; font-variant-numeric:tabular-nums; }}
  .ff-bullets {{ margin:10px 0 0; padding-left:18px; line-height:1.9; font-size:13px; }}
  .ff-bullets li {{ margin:2px 0; }}
  .muted {{ color:#9ca3af; font-size:13px; }}
  .footer {{ padding:16px 32px; color:#9ca3af; font-size:12px;
            border-top:1px solid #f3f4f6; line-height:1.6; }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <h1>🔍 {STOCK_NAME}（{STOCK_CODE}）每日复盘</h1>
      <div class="meta">{now} · {weekday} · 数据来源：东方财富</div>
    </div>
    <div class="legend">
      <span>均线排列提示：</span>
      <span><b class="bull">● 多头排列</b> 价≥MA5≥MA10≥MA20≥MA60（偏多）</span>
      <span><b class="bear">● 空头排列</b> 价≤MA5≤MA10≤MA20≤MA60（偏空）</span>
      <span><b class="mixed">● 均线纠结</b> 方向未明</span>
    </div>

    <div class="top">
      <div>
        <div class="name">{STOCK_NAME} <span class="code">{STOCK_CODE} · 创业板</span></div>
      </div>
      <div style="text-align:right">
        <span class="price-big" style="color:{c}">{fmt(r['price'])}</span>
        <span class="chg-big" style="color:{c}">{a} {r['pct']:+.2f}%</span>
      </div>
    </div>

    <div class="section">
      <h2>今日盘面</h2>
      <div class="kv-grid">
        {kv_chg}{kv_amt}{kv_close}{kv_open}{kv_high}{kv_low}{kv_prev}
        {kv_turn}{kv_volr}{kv_amp}{kv_amount}{kv_pe}{kv_tmv}{kv_fmv}
      </div>
    </div>

    <div class="section">
      <h2>均线 & 区间表现</h2>
      <div class="card" style="border-color:{edge}">
        <div class="badge-row">
          <span class="b-name">技术形态</span>
          <span class="badge" style="background:{badge_bg};color:{badge_fg}">{badge_label}</span>
        </div>
        <div class="ma-row">{ma_row}</div>
        <div class="rg-row">{range_row}</div>
      </div>
      <div class="note">{sp_html}</div>
    </div>

    <div class="section">
      <h2>基本面与政策面</h2>
      {render_fundamental(fa2, pa2, r)}
    </div>

    <div class="section">
      <h2>量能与资金</h2>
      <div class="note">{vol_html}</div>
      {render_fund_analysis(fa)}
      <div style="margin-top:12px">
        <div style="font-size:13px;color:#374151;margin-bottom:6px">近 10 日主力资金净流入（红=流入，绿=流出）</div>
        {ff_bars}
      </div>
    </div>

    <div class="footer">
      本报告由脚本于每个交易日盘后（16:30 北京时间）自动生成，仅供参考，不构成任何投资建议。<br>
      行情 / 日K（前复权）/ 主力资金流向均来自东方财富；均线 MA60 需约 60 个交易日数据积累后趋于稳定。
    </div>
  </div>
</body>
</html>"""
    return html


# ===================== 邮件发送（可选） =====================

def send_email(html):
    if not EMAIL_CONFIG.get("enable"):
        return
    cfg = EMAIL_CONFIG
    msg = MIMEMultipart("alternative")
    msg["From"] = cfg["sender"]
    msg["To"] = ", ".join(cfg["receivers"])
    msg["Subject"] = f"{STOCK_NAME}({STOCK_CODE}) 每日复盘 " + datetime.datetime.now().strftime("%Y-%m-%d")
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=15) as s:
            s.login(cfg["sender"], cfg["password"])
            s.sendmail(cfg["sender"], cfg["receivers"], msg.as_string())
        print("[OK] 邮件已发送")
    except Exception as e:  # noqa
        print(f"[WARN] 邮件发送失败: {e}")


# ===================== 主流程 =====================

def generate_review():
    print(f"[{datetime.datetime.now()}] 开始抓取 {STOCK_NAME}({STOCK_CODE}) 复盘数据...")
    r = build_review()
    if not r:
        print("[ERROR] 数据抓取失败，无法生成复盘")
        return None
    html = render_review(r)
    with open(REVIEW_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    send_email(html)
    print(f"[OK] 复盘已生成: {REVIEW_FILE}（排列：{r['arr']}）")
    return html


def run_once():
    generate_review()


def run_schedule():
    print(f"定时模式启动，将在每天 {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} 生成复盘。")
    while True:
        now = datetime.datetime.now()
        if now.hour == SCHEDULE_HOUR and now.minute == SCHEDULE_MINUTE:
            generate_review()
            time.sleep(60)
        else:
            time.sleep(20)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        run_once()
    else:
        run_schedule()
