#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股 / A股指数早报生成器
- 每日 08:00 从新浪财经抓取美股三大指数 + A股三大指数行情
- 生成一份自包含的 HTML 早报
- 支持两种运行模式：
    1) 定时模式（默认）：内部循环，每天 08:00 自动执行
    2) 单次模式：python morning_report.py once  立即生成一份报告

依赖：仅标准库 + requests（pip install requests）
若不想装 requests，可改用 urllib（见代码底部注释）。
"""

import os
import sys
import time
import smtplib
import datetime
import email.utils
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    import requests
except ImportError:
    print("缺少依赖 requests，请执行: pip install requests")
    sys.exit(1)


# ===================== 配置区 =====================

# 新浪财经行情接口（A股指数 / 美股指数）
SINA_API = "http://hq.sinajs.cn/list="

# 需要抓取的指数：代码 -> 名称分组
# A股指数
A_SHARE_INDICES = {
    "s_sh000001": "上证指数",
    "s_sz399001": "深证成指",
    "s_sz399006": "创业板指",
}
# 美股指数（int_ 前缀为美股盘前/收盘指数）
US_INDICES = {
    "int_dji": "道琼斯指数",
    "int_nasdaq": "纳斯达克指数",
    "int_sp500": "标普500指数",
}

# 新浪接口必须用该 Referer，否则返回 403
HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}

# 生成报告的输出路径（HTML 文件）
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_FILE = os.path.join(OUTPUT_DIR, "morning_report.html")

# 抓取重试次数
MAX_RETRY = 3
RETRY_INTERVAL = 5  # 秒

# 定时执行时间（24小时制）
SCHEDULE_HOUR = 8
SCHEDULE_MINUTE = 0

# 可选：生成后自动发送邮件（留空则不发送）
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
    """抓取新浪接口原始文本，返回 dict: code -> 原始字符串（不含 var 前缀）。"""
    url = SINA_API + ",".join(codes)
    last_err = None
    for _ in range(MAX_RETRY):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            # 新浪返回 GBK 编码
            text = resp.content.decode("gbk", errors="ignore")
            result = {}
            for line in text.strip().split(";\n"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                # 形如 var hq_str_s_sh000001="..."
                key_part, val_part = line.split("=", 1)
                code = key_part.replace("var", "").replace("hq_str_", "").strip()
                val = val_part.strip().strip('"')
                result[code] = val
            return result
        except Exception as e:  # noqa
            last_err = e
            time.sleep(RETRY_INTERVAL)
    print(f"[WARN] 抓取失败: {last_err}")
    return {}


def parse_index(code, raw, label):
    """解析单条指数数据，返回字典。"""
    if not raw:
        return {"code": code, "name": label, "price": "—", "change": "—",
                "pct": "—", "up": None}
    parts = raw.split(",")
    try:
        name = parts[0]
        price = float(parts[1])
        # A股格式 6 字段，美股格式 4 字段；涨跌额/涨跌幅索引不同
        if len(parts) >= 4:
            change = float(parts[2])
            pct = float(parts[3])
        else:
            change = pct = 0.0
        return {
            "code": code,
            "name": name or label,
            "price": price,
            "change": change,
            "pct": pct,
            "up": change > 0,
        }
    except (ValueError, IndexError):
        return {"code": code, "name": label, "price": "—", "change": "—",
                "pct": "—", "up": None}


def collect_data():
    """抓取并整合所有指数数据。"""
    a_codes = list(A_SHARE_INDICES.keys())
    u_codes = list(US_INDICES.keys())
    raw = fetch_raw(a_codes + u_codes)
    a_data = [parse_index(c, raw.get(c, ""), A_SHARE_INDICES[c]) for c in a_codes]
    u_data = [parse_index(c, raw.get(c, ""), US_INDICES[c]) for c in u_codes]
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
    return "#888888"      # 平/未知：灰


def arrow(up):
    if up is True:
        return "▲"
    if up is False:
        return "▼"
    return "—"


def render_rows(data):
    rows = []
    for d in data:
        c = color_style(d["up"])
        a = arrow(d["up"])
        chg = fmt(d["change"]) if isinstance(d["change"], float) else d["change"]
        pct = (fmt(d["pct"]) + "%") if isinstance(d["pct"], float) else d["pct"]
        chg_html = f'<span style="color:{c}">{a} {chg} ({pct})</span>' \
            if d["up"] is not None else '<span style="color:#888">—</span>'
        rows.append(f"""
        <tr>
          <td class="name">{d['name']}</td>
          <td class="price" style="color:{c}">{fmt(d['price'])}</td>
          <td class="chg">{chg_html}</td>
        </tr>""")
    return "\n".join(rows)


def build_html(a_data, u_data):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    weekday = ["星期一", "星期二", "星期三", "星期四", "星期五",
               "星期六", "星期日"][datetime.datetime.now().weekday()]
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>每日指数早报</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background:#f4f6fa; margin:0; padding:24px; color:#1f2937; }}
  .wrap {{ max-width:760px; margin:0 auto; background:#fff; border-radius:14px;
          overflow:hidden; box-shadow:0 6px 24px rgba(0,0,0,.08); }}
  .header {{ background:linear-gradient(135deg,#1e3a8a,#2563eb); color:#fff;
            padding:28px 32px; }}
  .header h1 {{ margin:0; font-size:22px; }}
  .header .meta {{ margin-top:8px; font-size:13px; opacity:.85; }}
  .section {{ padding:20px 32px; }}
  .section h2 {{ font-size:16px; margin:0 0 12px; color:#111827;
                border-left:4px solid #2563eb; padding-left:10px; }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; }}
  th {{ text-align:left; color:#6b7280; font-weight:500; padding:8px 6px;
       border-bottom:1px solid #eef2f7; font-size:12px; }}
  td {{ padding:12px 6px; border-bottom:1px solid #f3f4f6; }}
  .name {{ font-weight:600; }}
  .price {{ text-align:right; font-variant-numeric:tabular-nums; font-weight:600; }}
  .chg {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
  .footer {{ padding:16px 32px; color:#9ca3af; font-size:12px;
            border-top:1px solid #f3f4f6; }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <h1>📈 每日指数早报</h1>
      <div class="meta">{now} · {weekday} · 数据来源：新浪财经</div>
    </div>

    <div class="section">
      <h2>A 股指数</h2>
      <table>
        <thead><tr><th>指数</th><th style="text-align:right">最新</th>
        <th style="text-align:right">涨跌 / 涨跌幅</th></tr></thead>
        <tbody>
{render_rows(a_data)}
        </tbody>
      </table>
    </div>

    <div class="section">
      <h2>美股指数</h2>
      <table>
        <thead><tr><th>指数</th><th style="text-align:right">最新</th>
        <th style="text-align:right">涨跌 / 涨跌幅</th></tr></thead>
        <tbody>
{render_rows(u_data)}
        </tbody>
      </table>
    </div>

    <div class="footer">
      本报告由脚本于每日 08:00 自动生成，行情为上一交易日收盘数据，
      仅供参考，不构成任何投资建议。
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
    msg["Subject"] = "每日指数早报 " + datetime.datetime.now().strftime("%Y-%m-%d")
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
            # 避免一分钟内重复执行
            time.sleep(60)
        else:
            time.sleep(20)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        run_once()
    else:
        run_schedule()
