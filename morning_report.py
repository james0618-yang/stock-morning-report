#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股 / A股 板块指数 + 行业板块 早报生成器
- 每日 08:00 从新浪财经抓取美股、A股主要板块指数行情（直属行情）
- 从东方财富抓取 A股 行业板块（银行/半导体/医药…）实时行情（新浪行业板块接口已废弃）
- 同时计算各板块的 5日 / 10日 / 20日 均线（MA5/MA10/MA20）与区间涨跌幅
- 对均线排列（多头 / 空头 / 纠结）做醒目提示
- 生成一份自包含的 HTML 早报
- 支持两种运行模式：
    1) 定时模式（默认）：内部循环，每天 08:00 自动执行
    2) 单次模式：python morning_report.py once  立即生成一份报告

依赖：仅标准库 + requests（pip install requests）
"""

import os
import sys
import json
import time
import datetime
import smtplib
import email.utils
import urllib.request
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    import requests
except ImportError:
    print("缺少依赖 requests，请执行: pip install requests")
    sys.exit(1)


# ===================== 配置区 =====================

# 新浪财经实时行情接口（A股/美股指数）
SINA_API = "http://hq.sinajs.cn/list="

# 东方财富行情接口（行业板块实时 + 日K）；多 host 轮换以绕开偶发限流
EM_LIST_HOSTS = [
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://push2delay.eastmoney.com/api/qt/clist/get",
]
EM_KLINE_HOSTS = [
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://push2.eastmoney.com/api/qt/stock/kline/get",
]

# 新浪板块定义：code(实时) -> 名称；A股实时用 s_xxx，日K用去掉 s_ 的 xxx
A_SHARE_BOARDS = {
    "s_sh000001": "上证指数",
    "s_sz399001": "深证成指",
    "s_sz399006": "创业板指",
    "s_sh000300": "沪深300",
    "s_sh000016": "上证50",
    "s_sh000688": "科创50",
}
# 美股：实时用 int_xxx，历史日K 新浪不可用，改用脚本自维护缓存累积
US_BOARDS = {
    "int_dji": "道琼斯指数",
    "int_nasdaq": "纳斯达克指数",
    "int_sp500": "标普500指数",
}

# 东方财富行业板块（市值/代表性，secid 形如 90.BKxxxx）
INDUSTRIES = {
    "BK0475": "银行",
    "BK0473": "证券",
    "BK0474": "保险",
    "BK1036": "半导体",
    "BK0735": "计算机设备",
    "BK1211": "汽车",
    "BK1216": "医药生物",
    "BK0438": "食品饮料",
    "BK1202": "房地产",
    "BK1200": "电力设备",
    "BK0478": "有色金属",
    "BK0437": "煤炭",
}

# 新浪接口必须用该 Referer，否则返回 403
HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}
EM_HEADERS = {
    "Referer": "https://quote.eastmoney.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_FILE = os.path.join(OUTPUT_DIR, "morning_report.html")
US_HISTORY_FILE = os.path.join(OUTPUT_DIR, "us_history.json")

# 抓取重试：东方财富偶发限流，做多 host 轮换 + 空响应重试
MAX_RETRY = 5
RETRY_INTERVAL = 3  # 秒

# 定时执行时间（24小时制）
SCHEDULE_HOUR = 8
SCHEDULE_MINUTE = 0

# 可选：生成后自动发送邮件（生产中交由 GitHub Actions 的 SMTP 步骤发送）
EMAIL_CONFIG = {
    "enable": False,
    "smtp_host": "smtp.qq.com",
    "smtp_port": 465,
    "sender": "",
    "password": "",
    "receivers": [],
}


# ===================== 数据抓取 =====================

def fetch_raw(codes):
    """抓取新浪实时接口原始文本，返回 dict: code -> 原始字符串。"""
    if not codes:
        return {}
    url = SINA_API + ",".join(codes)
    last_err = None
    for _ in range(MAX_RETRY):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            text = resp.content.decode("gbk", errors="ignore")
            result = {}
            for line in text.strip().split(";\n"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                key_part, val_part = line.split("=", 1)
                code = key_part.replace("var", "").replace("hq_str_", "").strip()
                val = val_part.strip().strip('"').rstrip(';').strip('"')
                result[code] = val
            if not result:
                last_err = "解析为空(可能为限流/拦截)"
                time.sleep(RETRY_INTERVAL)
                continue
            return result
        except Exception as e:  # noqa
            last_err = e
            time.sleep(RETRY_INTERVAL)
    print(f"[WARN] 实时行情抓取失败: {last_err}")
    return {}


def fetch_kline_a_share(symbol, datalen=40):
    """A股板块历史日K收盘价序列（升序），用于计算均线。失败返回 None。"""
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}")
    for _ in range(MAX_RETRY):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            if isinstance(data, list) and data:
                closes = [float(x["close"]) for x in data if x.get("close")]
                return closes if closes else None
        except Exception as e:  # noqa
            last_err = e
            time.sleep(RETRY_INTERVAL)
    print(f"[WARN] A股日K抓取失败 {symbol}: {last_err}")
    return None


_em_session = requests.Session()


def _em_get_json(hosts, params, timeout=12, retries=None, interval=None):
    """东方财富接口：多 host 轮换 + 空响应/非JSON 重试，返回 json dict；全失败返回 None。
    retries/interval 可覆盖全局，便于对易限流的接口(kline)做轻量重试，避免本地空转。"""
    retries = retries or MAX_RETRY
    interval = interval or RETRY_INTERVAL
    last_err = None
    for i in range(retries):
        host = hosts[i % len(hosts)]
        try:
            resp = _em_session.get(host, params=params, headers=EM_HEADERS, timeout=timeout)
            if not resp.content:
                last_err = "空响应(可能被限流)"
                time.sleep(interval)
                continue
            return resp.json()
        except Exception as e:  # noqa
            last_err = e
            time.sleep(interval)
    print(f"[WARN] 东方财富请求失败 {hosts[0]}: {last_err}")
    return None


def fetch_industry_realtime():
    """东方财富行业板块实时行情，返回 dict: BK代码 -> {name,price,pct,leader,leader_pct,main_inflow}。"""
    # 一次性拉取全部行业板块，再按配置的 BK 集合筛选
    params = {
        "pn": 1, "pz": 400, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fs": "m:90+t:2+f:!50", "fields": "f12,f14,f2,f3,f128,f184,f62",
    }
    d = _em_get_json(EM_LIST_HOSTS, params)
    diff = d.get("data", {}).get("diff", []) if d else None
    if not diff:
        print("[WARN] 行业板块实时抓取失败（空数据）")
        return {}
    out = {}
    for x in diff:
        bk = x.get("f12")
        if bk not in INDUSTRIES:
            continue
        f2 = x.get("f2")
        f3 = x.get("f3")
        out[bk] = {
            "name": INDUSTRIES[bk],
            "price": float(f2) if isinstance(f2, (int, float)) else None,
            "pct": float(f3) if isinstance(f3, (int, float)) else None,
            "leader": x.get("f128") or "—",
            "leader_pct": (float(x["f184"]) if isinstance(x.get("f184"), (int, float)) else None),
            "main_inflow": (float(x["f62"]) / 1e8 if isinstance(x.get("f62"), (int, float)) else None),
        }
    return out


def fetch_industry_kline(bk, datalen=40):
    """东方财富行业板块日K收盘价序列（升序），secid=90.BKxxxx。失败返回 None。"""
    params = {
        "secid": f"90.{bk}",
        "fields1": "f1",
        "fields2": "f51,f53",
        "klt": 101,
        "fqt": 0,
        "end": "20500101",
        "lmt": datalen,
    }
    d = _em_get_json(EM_KLINE_HOSTS, params, retries=2, interval=1)
    k = d.get("data") if d else None
    if k and k.get("klines"):
        closes = [float(x.split(",")[1]) for x in k["klines"]
                  if len(x.split(",")) > 1]
        return closes if closes else None
    print(f"[WARN] 行业板块日K抓取失败 {bk}（MA 将显示为空）")
    return None


def load_us_history():
    if os.path.exists(US_HISTORY_FILE):
        try:
            with open(US_HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_us_history(hist):
    try:
        with open(US_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa
        print(f"[WARN] 美股历史缓存写入失败: {e}")


def update_us_history(hist, code, date_str, close):
    lst = hist.setdefault(code, [])
    for item in lst:
        if item["d"] == date_str:
            item["c"] = close
            break
    else:
        lst.append({"d": date_str, "c": close})
    lst.sort(key=lambda x: x["d"])
    hist[code] = lst[-240:]


# ===================== 指标计算 =====================

def calc_ma(closes, n):
    if closes and len(closes) >= n:
        return sum(closes[-n:]) / n
    return None


def calc_n_chg(closes, n):
    """近 n 日涨跌幅（%）：最新收盘 vs n 个交易日前收盘。"""
    if closes and len(closes) > n and closes[-1 - n]:
        return (closes[-1] / closes[-1 - n] - 1) * 100
    return None


def ma_arrangement(price, ma5, ma10, ma20):
    """均线排列：多头(price≥MA5≥MA10≥MA20) / 空头(相反) / 纠结 / 数据不足。"""
    if not all(isinstance(v, float) for v in (price, ma5, ma10, ma20)):
        return ("数据不足", "neutral")
    if price >= ma5 >= ma10 >= ma20:
        return ("多头排列", "bull")
    if price <= ma5 <= ma10 <= ma20:
        return ("空头排列", "bear")
    return ("均线纠结", "mixed")


# ===================== 解析 =====================

def parse_sina_board(code, raw, label):
    """解析新浪单条板块实时数据。A股 s_ 6字段；美股 int_ 4字段。"""
    if not raw:
        return {"code": code, "name": label, "price": None, "pct": None, "up": None}
    parts = raw.split(",")
    try:
        name = parts[0]
        price = float(parts[1])
        pct = float(parts[3]) if len(parts) > 3 else 0.0
        return {"code": code, "name": name or label, "price": price,
                "pct": pct, "up": pct > 0}
    except (ValueError, IndexError):
        return {"code": code, "name": label, "price": None, "pct": None, "up": None}


def attach_indicators(d, closes):
    d["ma5"] = calc_ma(closes, 5)
    d["ma10"] = calc_ma(closes, 10)
    d["ma20"] = calc_ma(closes, 20)
    d["chg5"] = calc_n_chg(closes, 5)
    d["chg10"] = calc_n_chg(closes, 10)
    d["chg20"] = calc_n_chg(closes, 20)
    d["_closes"] = closes
    d["arr"], d["arr_cls"] = ma_arrangement(d["price"], d["ma5"], d["ma10"], d["ma20"])


def collect_a_share():
    raw = fetch_raw(list(A_SHARE_BOARDS.keys()))
    data = []
    for code, name in A_SHARE_BOARDS.items():
        d = parse_sina_board(code, raw.get(code, ""), name)
        closes = fetch_kline_a_share(code.replace("s_", ""))
        attach_indicators(d, closes)
        data.append(d)
    return data


def collect_us():
    raw = fetch_raw(list(US_BOARDS.keys()))
    data = []
    hist = load_us_history()
    today = datetime.date.today().strftime("%Y-%m-%d")
    for code, name in US_BOARDS.items():
        d = parse_sina_board(code, raw.get(code, ""), name)
        if isinstance(d["price"], float):
            update_us_history(hist, code, today, d["price"])
        closes = [x["c"] for x in hist.get(code, [])]
        attach_indicators(d, closes)
        data.append(d)
    save_us_history(hist)
    return data


def collect_industry():
    rt = fetch_industry_realtime()
    data = []
    for bk, label in INDUSTRIES.items():
        info = rt.get(bk, {})
        price = info.get("price")
        pct = info.get("pct")
        d = {
            "code": bk,
            "name": label,
            "price": price,
            "pct": pct,
            "up": (pct > 0) if isinstance(pct, float) else None,
            "leader": info.get("leader", "—"),
            "leader_pct": info.get("leader_pct"),
            "main_inflow": info.get("main_inflow"),
        }
        closes = fetch_industry_kline(bk)
        attach_indicators(d, closes)
        data.append(d)
        time.sleep(0.15)  # 轻量节流，避免东方财富瞬时限流
    return data


# ===================== HTML 生成 =====================

def fmt(v, digits=2):
    if isinstance(v, float):
        return f"{v:,.{digits}f}"
    return str(v)


def color_style(up):
    if up is True:
        return "#e23b3b"   # 涨：红
    if up is False:
        return "#16a34a"  # 跌：绿
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

# 排列对应卡片边框色
CARD_EDGE = {"bull": "#e23b3b", "bear": "#16a34a", "mixed": "#f59e0b", "neutral": "#eef2f7"}


def render_board_card(d):
    """渲染单个宽基/美股板块卡片（含均线排列醒目徽章）。"""
    up = d["up"]
    c = color_style(up)
    a = arrow(up)
    price = fmt(d["price"]) if isinstance(d["price"], float) else "—"
    pct = (fmt(d["pct"]) + "%") if isinstance(d["pct"], float) else "—"
    chg_html = (f'<span style="color:{c}">{a} {pct}</span>'
                if d["up"] is not None else '<span style="color:#888">—</span>')

    badge_label, badge_bg, badge_fg = BADGE.get(d["arr_cls"], BADGE["neutral"])

    def ma_cell(n, ma):
        if isinstance(ma, float):
            pos_c = ma_position_color(d["price"], ma)
            pc = "站上" if (isinstance(d["price"], float) and d["price"] >= ma) else "跌破"
            return (f'<div class="ma"><span class="ma-n">MA{n}</span>'
                    f'<span class="ma-v" style="color:{pos_c}">{fmt(ma)}</span>'
                    f'<span class="ma-tag" style="color:{pos_c}">{pc}</span></div>')
        return (f'<div class="ma"><span class="ma-n">MA{n}</span>'
                f'<span class="ma-v" style="color:#888">—</span></div>')

    ma_row = "".join(ma_cell(n, d.get(f"ma{n}")) for n in (5, 10, 20))

    def range_cell(label, v):
        if isinstance(v, float):
            vc = "#e23b3b" if v >= 0 else "#16a34a"
            return (f'<div class="rg"><span class="rg-n">{label}</span>'
                    f'<span class="rg-v" style="color:{vc}">{v:+.2f}%</span></div>')
        return (f'<div class="rg"><span class="rg-n">{label}</span>'
                f'<span class="rg-v" style="color:#888">—</span></div>')

    range_row = "".join(range_cell(lbl, d.get(k))
                        for lbl, k in (("近5日", "chg5"), ("近10日", "chg10"), ("近20日", "chg20")))

    edge = CARD_EDGE.get(d["arr_cls"], "#eef2f7")
    return f"""
    <div class="card" style="border-color:{edge}">
      <div class="badge-row">
        <span class="b-name">{d['name']}</span>
        <span class="badge" style="background:{badge_bg};color:{badge_fg}">{badge_label}</span>
      </div>
      <div class="card-h">
        <span class="b-price" style="color:{c}">{price}</span>
        <span class="card-chg" style="color:{c}">{chg_html}</span>
      </div>
      <div class="ma-row">{ma_row}</div>
      <div class="rg-row">{range_row}</div>
    </div>"""


def render_industry_card(d):
    """渲染行业板块卡片（紧凑，含领涨股 + 主力净流入 + 均线排列醒目徽章）。"""
    up = d["up"]
    c = color_style(up)
    a = arrow(up)
    price = fmt(d["price"]) if isinstance(d["price"], float) else "—"
    pct = (fmt(d["pct"]) + "%") if isinstance(d["pct"], float) else "—"
    chg_html = (f'{a} <span style="color:{c}">{pct}</span>'
                if d["up"] is not None else '<span style="color:#888">—</span>')

    badge_label, badge_bg, badge_fg = BADGE.get(d["arr_cls"], BADGE["neutral"])

    # 领涨股
    lp = d.get("leader_pct")
    if isinstance(lp, float):
        lc = "#e23b3b" if lp >= 0 else "#16a34a"
        leader_html = f'领涨：{d.get("leader","—")} <span style="color:{lc}">{lp:+.2f}%</span>'
    else:
        leader_html = f'领涨：{d.get("leader","—")}'

    # 主力净流入（亿）
    mi = d.get("main_inflow")
    if isinstance(mi, float):
        mic = "#e23b3b" if mi >= 0 else "#16a34a"
        mi_html = f'主力净流入 <span style="color:{mic}">{mi:+.2f}亿</span>'
    else:
        mi_html = '主力净流入 <span style="color:#888">—</span>'

    def ma_cell(n, ma):
        if isinstance(ma, float):
            pos_c = ma_position_color(d["price"], ma)
            pc = "站上" if (isinstance(d["price"], float) and d["price"] >= ma) else "跌破"
            return (f'<div class="ma"><span class="ma-n">MA{n}</span>'
                    f'<span class="ma-v" style="color:{pos_c}">{fmt(ma)}</span>'
                    f'<span class="ma-tag" style="color:{pos_c}">{pc}</span></div>')
        return (f'<div class="ma"><span class="ma-n">MA{n}</span>'
                f'<span class="ma-v" style="color:#888">—</span></div>')

    ma_row = "".join(ma_cell(n, d.get(f"ma{n}")) for n in (5, 10, 20))

    def range_cell(label, v):
        if isinstance(v, float):
            vc = "#e23b3b" if v >= 0 else "#16a34a"
            return (f'<div class="rg"><span class="rg-n">{label}</span>'
                    f'<span class="rg-v" style="color:{vc}">{v:+.2f}%</span></div>')
        return (f'<div class="rg"><span class="rg-n">{label}</span>'
                f'<span class="rg-v" style="color:#888">—</span></div>')

    range_row = "".join(range_cell(lbl, d.get(k))
                        for lbl, k in (("近5日", "chg5"), ("近10日", "chg10"), ("近20日", "chg20")))

    edge = CARD_EDGE.get(d["arr_cls"], "#eef2f7")
    return f"""
    <div class="card ind" style="border-color:{edge}">
      <div class="badge-row">
        <span class="b-name">{d['name']}</span>
        <span class="badge" style="background:{badge_bg};color:{badge_fg}">{badge_label}</span>
      </div>
      <div class="card-h">
        <span class="b-price" style="color:{c}">{price}</span>
        <span class="card-chg">{chg_html}</span>
      </div>
      <div class="extra">{leader_html} &nbsp;·&nbsp; {mi_html}</div>
      <div class="ma-row">{ma_row}</div>
      <div class="rg-row">{range_row}</div>
    </div>"""


def build_html(a_data, u_data, i_data):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    weekday = ["星期一", "星期二", "星期三", "星期四", "星期五",
               "星期六", "星期日"][datetime.datetime.now().weekday()]
    us_days = max((len(d["_closes"]) for d in u_data if d.get("_closes")), default=0)
    note = ("行情为上一交易日收盘数据；美股均线基于脚本逐日累积的本地历史，"
            f"当前已累积 {us_days} 个交易日，约 20 个交易日后 MA20 趋于稳定。"
            ) if us_days < 20 else "行情为上一交易日收盘数据。"
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>每日板块指数早报</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background:#f4f6fa; margin:0; padding:24px; color:#1f2937; }}
  .wrap {{ max-width:880px; margin:0 auto; background:#fff; border-radius:14px;
          overflow:hidden; box-shadow:0 6px 24px rgba(0,0,0,.08); }}
  .header {{ background:linear-gradient(135deg,#1e3a8a,#2563eb); color:#fff;
            padding:28px 32px; }}
  .header h1 {{ margin:0; font-size:22px; }}
  .header .meta {{ margin-top:8px; font-size:13px; opacity:.85; }}
  .legend {{ background:#eef2ff; color:#3730a3; font-size:12px;
            padding:8px 32px; display:flex; gap:16px; flex-wrap:wrap; }}
  .legend b.bull {{ color:#e23b3b; }}
  .legend b.bear {{ color:#16a34a; }}
  .legend b.mixed {{ color:#d97706; }}
  .section {{ padding:18px 32px; }}
  .section h2 {{ font-size:16px; margin:6px 0 14px; color:#111827;
                border-left:4px solid #2563eb; padding-left:10px; }}
  .card {{ border:1px solid #eef2f7; border-left-width:4px; border-radius:12px;
          padding:14px 16px; margin-bottom:12px; background:#fafbfc; }}
  .ind {{ background:#fff; }}
  .badge-row {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; }}
  .b-name {{ font-size:15px; font-weight:700; }}
  .badge {{ font-size:11px; font-weight:700; padding:3px 10px; border-radius:999px; white-space:nowrap; }}
  .card-h {{ display:flex; justify-content:space-between; align-items:baseline; }}
  .b-price {{ font-size:20px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .card-chg {{ font-size:13px; font-variant-numeric:tabular-nums; }}
  .extra {{ font-size:12px; color:#4b5563; margin-top:6px; }}
  .ma-row {{ display:flex; gap:10px; margin-top:12px; flex-wrap:wrap; }}
  .ma {{ flex:1; min-width:84px; background:#fff; border:1px solid #eef2f7;
        border-radius:8px; padding:8px 10px; }}
  .ma-n {{ display:block; font-size:11px; color:#9ca3af; }}
  .ma-v {{ display:block; font-size:15px; font-weight:700;
          font-variant-numeric:tabular-nums; }}
  .ma-tag {{ font-size:11px; }}
  .rg-row {{ display:flex; gap:10px; margin-top:10px; flex-wrap:wrap; }}
  .rg {{ flex:1; min-width:84px; text-align:center; background:#fff;
        border:1px solid #eef2f7; border-radius:8px; padding:8px 6px; }}
  .rg-n {{ display:block; font-size:11px; color:#9ca3af; }}
  .rg-v {{ display:block; font-size:14px; font-weight:700;
          font-variant-numeric:tabular-nums; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr));
          gap:12px; }}
  .grid .card {{ margin-bottom:0; }}
  .footer {{ padding:16px 32px; color:#9ca3af; font-size:12px;
            border-top:1px solid #f3f4f6; line-height:1.6; }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <h1>📊 每日板块指数早报</h1>
      <div class="meta">{now} · {weekday} · 数据来源：新浪财经 / 东方财富</div>
    </div>
    <div class="legend">
      <span>均线排列提示：</span>
      <span><b class="bull">● 多头排列</b> 价≥MA5≥MA10≥MA20（偏多）</span>
      <span><b class="bear">● 空头排列</b> 价≤MA5≤MA10≤MA20（偏空）</span>
      <span><b class="mixed">● 均线纠结</b> 方向未明</span>
    </div>

    <div class="section">
      <h2>A 股板块指数</h2>
      {''.join(render_board_card(d) for d in a_data)}
    </div>

    <div class="section">
      <h2>美股板块指数</h2>
      {''.join(render_board_card(d) for d in u_data)}
    </div>

    <div class="section">
      <h2>A 股行业板块</h2>
      <div class="grid">
        {''.join(render_industry_card(d) for d in i_data)}
      </div>
    </div>

    <div class="footer">
      本报告由脚本于每日 08:00 自动生成，仅供参考，不构成任何投资建议。<br>
      {note}<br>
      A股/美股宽基指数来自新浪财经；行业板块实时行情与日K来自东方财富（新浪行业板块接口已废弃）。
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
    msg["Subject"] = "每日板块指数早报 " + datetime.datetime.now().strftime("%Y-%m-%d")
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

def generate_report():
    print(f"[{datetime.datetime.now()}] 开始抓取行情...")
    a_data = collect_a_share()
    u_data = collect_us()
    i_data = collect_industry()
    # 完全未抓到任何数据，视为失败，不生成报告
    if not a_data and not u_data and not i_data:
        print("[ERROR] 所有行情数据抓取失败，未生成报告")
        return None
    html = build_html(a_data, u_data, i_data)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    send_email(html)
    print(f"[OK] 早报已生成: {REPORT_FILE}（A股{len(a_data)} 美股{len(u_data)} 行业{len(i_data)}）")
    return html


def run_once():
    if not generate_report():
        sys.exit(1)


def run_schedule():
    print(f"定时模式启动，将在每天 {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} 生成早报。")
    while True:
        now = datetime.datetime.now()
        if now.hour == SCHEDULE_HOUR and now.minute == SCHEDULE_MINUTE:
            generate_report()
            time.sleep(60)
        else:
            time.sleep(20)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        run_once()
    else:
        run_schedule()
