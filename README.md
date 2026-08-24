# stock-morning-report

每日美股 / A 股指数早报生成脚本。

## 功能
- 每日 08:00 从新浪财经抓取美股三大指数（道琼斯 / 纳斯达克 / 标普500）与 A 股三大指数（上证 / 深证成指 / 创业板指）
- 自动生成自包含 HTML 早报 `morning_report.html`（红涨绿跌，含涨跌额与涨跌幅，移动端自适应）
- 失败自动重试，空数据占位不崩溃
- 可选邮件推送（配置 `EMAIL_CONFIG`）

## 使用
```bash
pip install requests

# 立即生成一次
python3 morning_report.py once

# 定时模式（脚本内部循环，每天 08:00）
python3 morning_report.py
```

## 生产部署（推荐 cron）
```bash
# crontab -e
0 8 * * * cd /脚本目录 && /usr/bin/python3 morning_report.py once >> report.log 2>&1
```

## 数据源
新浪财经行情接口 `hq.sinajs.cn`（已带 Referer 头绕过 403）。

> 行情为上一交易日收盘数据，仅供参考，不构成投资建议。
