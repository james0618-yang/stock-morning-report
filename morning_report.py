#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股 / A股 板块指数早报生成器
- 每日 08:00 从新浪财经抓取美股、A股各板块指数行情（直属行情）
- 同时计算各板块的 5日 / 10日 / 20日 均线（MA5/MA10/MA20）与区间涨跌幅
- 生成一份自包含的 HTML 早报
- 支持两种运行模式：
    1) 定时模式（默认）：内部循环，每天 08:00 自动执行
    2) 单次模式：python morning_report.py once  立即生成一份报告

依赖：仅标准库 + requests（pip install requests）
若不想装 requests，可把 fetch_raw 改用 urllib（见代码注释）。
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

# 板块定义：code(实时) -> (名称, 市场, K线symbol)
#   A股：实时用 s_xxx，日K线用去掉 s_ 前缀的 xxx（新浪 CN_MarketData 接口）
#  美股：实时用 int_xxx，历史日K 新浪当前不可用，改用脚本自维护缓存累积
A_SHARE_BOARDS = {
    "s_sh000001": ("上证指数", "sh000001"),
    "s_sz399001": ("深证成指", "sz399001"),
    "s_sz399006": ("创业板指", "sz399006"),
    "s_sh000300": ("沪深300",  "sh000300"),
    "s_sh000016": ("上证50",   "sh000016"),
    "s_sh000688": ("科创50",   "sh000688"),
}
US_BOARDS = {
    "int_dji":     ("道琼斯指数", None),
    "int_nasdaq":  ("纳斯达克指数", None),
    "int_sp500":   ("标普500指数", None),
}

# 新浪接口必须用该 Referer，否则返回 403
HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_FILE = os.path.join(OUTPUT_DIR, "morning_report.html")
US_HISTORY_FILE = os.path.join(OUTPUT_DIR, "us_history.json")

# 抓取重试次数
MAX_RETRY = 3
RETRY_INTERVAL = 5  # 秒

# 定时执行时间（24小时制）
SCHEDULE_HOUR = 8
SCHEDULE_MINUTE = 0

# 可选：生成后自动发送邮件（留空则不发送；生产中交由 GitHub Actions 的 SMTP 步骤发送）
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
                # 新浪返回末行常带结尾分号，需一并剥离，否则末位标的历史字段解析失败
                val = val_part.strip().strip('"').rstrip(';').strip('"')
                result[code] = val
            return result
        except Exception as e:  # noqa
            last_err = e
            time.sleep(RETRY_INTERVAL)
    print(f"[WARN] 实时行情抓取失败: {last_err}")
    return {}


def fetch_kline_a_share(symbol, datalen=40):
    """抓取 A股板块历史日K收盘价序列（升序），用于计算均线。失败返回 None。"""
    if not symbol:
        return None
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        if isinstance(data, list) and data:
            closes = [float(x["close"]) for x in data if x.get("close")]
            return closes if closes else None
    except Exception as e:  # noqa
        print(f"[WARN] A股日K抓取失败 {symbol}: {e}")
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
    """把当日美股收盘点位追加/更新进缓存，按日期排序并截断到最近 240 条。"""
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


# ===================== 解析 =====================

def parse_board(code, raw, label):
    """解析单条板块实时数据。A股 s_ 6字段；美股 int_ 4字段。"""
    if not raw:
        return {"code": code, "name": label, "price": None, "change": None,
                "pct": None, "up": None}
    parts = raw.split(",")
    try:
        name = parts[0]
        price = float(parts[1])
        change = float(parts[2]) if len(parts) > 2 else 0.0
        pct = float(parts[3]) if len(parts) > 3 else 0.0
        return {
            "code": code,
            "name": name or label,
            "price": price,
            "change": change,
            "pct": pct,
            "up": change > 0,
        }
    except (ValueError, IndexError):
        return {"code": code, "name": label, "price": None, "change": None,
                "pct": None, "up": None}


def attach_indicators(d, closes):
    d["ma5"] = calc_ma(closes, 5)
    d["ma10"] = calc_ma(closes, 10)
    d["ma20"] = calc_ma(closes, 20)
    d["chg5"] = calc_n_chg(closes, 5)
    d["chg10"] = calc_n_chg(closes, 10)
    d["chg20"] = calc_n_chg(closes, 20)
    d["_closes"] = closes  # 内部用，渲染时忽略


def collect_data():
    """抓取并整合所有板块数据。"""
    a_codes = list(A_SHARE_BOARDS.keys())
    u_codes = list(US_BOARDS.keys())
    raw = fetch_raw(a_codes + u_codes)

    a_data = []
    for code, (name, ksym) in A_SHARE_BOARDS.items():
        d = parse_board(code, raw.get(code, ""), name)
        closes = fetch_kline_a_share(ksym)
        attach_indicators(d, closes)
        a_data.append(d)

    u_data = []
    hist = load_us_history()
    today = datetime.date.today().strftime("%Y-%m-%d")
    for code, (name, _ksym) in US_BOARDS.items():
        d = parse_board(code, raw.get(code, ""), name)
        if isinstance(d["price"], float):
            update_us_history(hist, code, today, d["price"])
        closes = [x["c"] for x in hist.get(code, [])]
        attach_indicators(d, closes)
        u_data.append(d)
    save_us_history(hist)

    return a_data, u_data


# ===================== HTML 生成 =====================

def fmt(v, digits=2):
    if isinstance(v, float):
        return f"{v:,.{digits}f}"
    return str(v)


def color_style(up):
    if up is True:
        return "#e23b3b"   # 涨：红（中国市场习惯）
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
    """当前价相对均线的位置：站上=偏多(红)，跌破=偏空(绿)，未知=灰。"""
    if isinstance(price, float) and isinstance(ma, float):
        return "#e23b3b" if price >= ma else "#16a34a"
    return "#888888"


def render_board_card(d):
    """渲染单个板块卡片。"""
    up = d["up"]
    c = color_style(up)
    a = arrow(up)
    price = fmt(d["price"]) if isinstance(d["price"], float) else "—"
    chg = fmt(d["change"]) if isinstance(d["change"], float) else "—"
    pct = (fmt(d["pct"]) + "%") if isinstance(d["pct"], float) else "—"

    chg_html = (f'<span style="color:{c}">{a} {chg} ({pct})</span>'
                if d["up"] is not None else '<span style="color:#888">—</span>')

    # 均线行
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

    # 区间涨跌行
    def range_cell(label, v):
        if isinstance(v, float):
            vc = "#e23b3b" if v >= 0 else "#16a34a"
            return (f'<div class="rg"><span class="rg-n">{label}</span>'
                    f'<span class="rg-v" style="color:{vc}">{v:+.2f}%</span></div>')
        return (f'<div class="rg"><span class="rg-n">{label}</span>'
                f'<span class="rg-v" style="color:#888">—</span></div>')

    range_row = "".join(range_cell(lbl, d.get(k))
                        for lbl, k in (("近5日", "chg5"), ("近10日", "chg10"), ("近20日", "chg20")))

    return f"""
    <div class="card">
      <div class="card-h">
        <span class="b-name">{d['name']}</span>
        <span class="b-price" style="color:{c}">{price}</span>
      </div>
      <div class="card-chg">{chg_html}</div>
      <div class="ma-row">{ma_row}</div>
      <div class="rg-row">{range_row}</div>
    </div>"""


def build_html(a_data, u_data):
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
  .wrap {{ max-width:780px; margin:0 auto; background:#fff; border-radius:14px;
          overflow:hidden; box-shadow:0 6px 24px rgba(0,0,0,.08); }}
  .header {{ background:linear-gradient(135deg,#1e3a8a,#2563eb); color:#fff;
            padding:28px 32px; }}
  .header h1 {{ margin:0; font-size:22px; }}
  .header .meta {{ margin-top:8px; font-size:13px; opacity:.85; }}
  .section {{ padding:18px 32px; }}
  .section h2 {{ font-size:16px; margin:6px 0 14px; color:#111827;
                border-left:4px solid #2563eb; padding-left:10px; }}
  .card {{ border:1px solid #eef2f7; border-radius:12px; padding:14px 16px;
          margin-bottom:12px; background:#fafbfc; }}
  .card-h {{ display:flex; justify-content:space-between; align-items:baseline; }}
  .b-name {{ font-size:15px; font-weight:700; }}
  .b-price {{ font-size:20px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .card-chg {{ text-align:right; font-size:13px; margin-top:2px;
              font-variant-numeric:tabular-nums; }}
  .ma-row {{ display:flex; gap:10px; margin-top:12px; flex-wrap:wrap; }}
  .ma {{ flex:1; min-width:90px; background:#fff; border:1px solid #eef2f7;
        border-radius:8px; padding:8px 10px; }}
  .ma-n {{ display:block; font-size:11px; color:#9ca3af; }}
  .ma-v {{ display:block; font-size:15px; font-weight:700;
          font-variant-numeric:tabular-nums; }}
  .ma-tag {{ font-size:11px; }}
  .rg-row {{ display:flex; gap:10px; margin-top:10px; flex-wrap:wrap; }}
  .rg {{ flex:1; min-width:90px; text-align:center; background:#fff;
        border:1px solid #eef2f7; border-radius:8px; padding:8px 6px; }}
  .rg-n {{ display:block; font-size:11px; color:#9ca3af; }}
  .rg-v {{ display:block; font-size:14px; font-weight:700;
          font-variant-numeric:tabular-nums; }}
  .footer {{ padding:16px 32px; color:#9ca3af; font-size:12px;
            border-top:1px solid #f3f4f6; line-height:1.6; }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <h1>📊 每日板块指数早报</h1>
      <div class="meta">{now} · {weekday} · 数据来源：新浪财经</div>
    </div>

    <div class="section">
      <h2>A 股板块</h2>
      {''.join(render_board_card(d) for d in a_data)}
    </div>

    <div class="section">
      <h2>美股板块</h2>
      {''.join(render_board_card(d) for d in u_data)}
    </div>

    <div class="footer">
      本报告由脚本于每日 08:00 自动生成，仅供参考，不构成任何投资建议。<br>
      {note}
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
    a_data, u_data = collect_data()
    html = build_html(a_data, u_data)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    send_email(html)
    print(f"[OK] 早报已生成: {REPORT_FILE}")
    return html


def run_once():
    generate_report()


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
