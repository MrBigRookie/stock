#!/usr/bin/env python3
"""
财经资讯分析 & 潜力股挖掘工具
数据源：AKShare（免费开源）
推送：Bark（iOS）

用法：
  source .venv/bin/activate
  python stock_monitor.py              # 单次运行（默认）
  python stock_monitor.py --schedule   # 定时运行（默认交易日下午15:30）
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from pathlib import Path

import requests
import pandas as pd

try:
    import akshare as ak
except ImportError:
    print("请先安装 akshare: uv pip install akshare")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass


# ── 配置 ──────────────────────────────────────────────
CONFIG = {
    "bark_key": os.getenv("BARK_KEY", ""),
    "bark_url": os.getenv("BARK_URL", "https://api.day.app"),
    "news_days": int(os.getenv("NEWS_DAYS", "3")),
    "top_n": int(os.getenv("TOP_N", "15")),
    "schedule_time": os.getenv("SCHEDULE_TIME", "15:30"),
}

# 关键词配置：板块 -> 关键词列表
KEYWORDS = {
    "新能源": [
        "新能源", "光伏", "锂电", "锂电池", "储能", "风电", "氢能", "钠离子",
        "固态电池", "钙钛矿", "充电桩", "特高压", "逆变器", "宁德时代", "比亚迪",
        "隆基绿能", "通威股份", "阳光电源", "亿纬锂能", "天齐锂业", "赣锋锂业",
        "晶澳科技", "天合光能", "TCL中环", "先导智能", "恩捷股份", "璞泰来",
        "鹏辉能源", "国轩高科", "欣旺达", "德方纳米", "当升科技", "容百科技",
    ],
    "AI/科技": [
        "人工智能", "AI", "大模型", "算力", "GPU", "芯片", "半导体", "光刻机",
        "自动驾驶", "机器人", "具身智能", "Sora", "ChatGPT", "Copilot",
        "寒武纪", "海光信息", "中芯国际", "中科曙光", "浪潮信息", "工业富联",
        "科大讯飞", "商汤", "百度", "华为", "昆仑万维", "360",
        "中际旭创", "新易盛", "天孚通信", "沪电股份", "胜宏科技",
    ],
    "资金面": [
        "北向资金", "外资", "沪股通", "深股通", "净流入", "净买入",
    ],
}

NEGATIVE_WORDS = [
    "下跌", "暴跌", "跌停", "亏损", "减持", "暴雷", "退市", "处罚", "违规",
    "诉讼", "债务违约", "业绩下滑", "预亏", "风险提示", "监管", "问询函",
]
POSITIVE_WORDS = [
    "上涨", "涨停", "增长", "突破", "创新高", "增持", "盈利", "超预期",
    "利好", "中标", "签约", "量产", "交付", "扩产", "投资",
]

SCRIPT_DIR = Path(__file__).parent


# ── 数据采集 ──────────────────────────────────────────

def _safe_call(fn, label, retries=2, **kwargs):
    """安全调用 API，支持重试，失败时返回 None"""
    for attempt in range(retries + 1):
        try:
            result = fn(**kwargs)
            if result is None or (hasattr(result, 'empty') and result.empty):
                return None
            return result
        except Exception as e:
            if attempt < retries:
                time.sleep(1.5)
                continue
            print(f"  [WARN] {label} 获取失败: {e}")
            return None


def fetch_news():
    """采集财联社 + 东方财富新闻"""
    all_news = []

    # 财联社电报 — 列名: 标题, 内容, 发布日期, 发布时间
    df = _safe_call(ak.stock_info_global_cls, "财联社电报")
    if df is not None:
        for _, row in df.iterrows():
            date_str = str(row.get("发布日期", ""))
            time_str = str(row.get("发布时间", ""))
            all_news.append({
                "title": str(row.get("标题", "")),
                "content": str(row.get("内容", "")),
                "time": f"{date_str} {time_str}".strip(),
                "source": "财联社",
            })

    # 东方财富快讯 — 列名: 标题, 摘要, 发布时间, 链接
    df = _safe_call(ak.stock_info_global_em, "东方财富快讯")
    if df is not None:
        for _, row in df.iterrows():
            all_news.append({
                "title": str(row.get("标题", "")),
                "content": str(row.get("摘要", "")),
                "time": str(row.get("发布时间", "")),
                "source": "东方财富",
            })

    return all_news


def fetch_north_flow():
    """获取北向资金近期流向（优先净买额，回退到持股市值变化估算）"""
    result = {"daily": None, "summary": ""}
    df = _safe_call(ak.stock_hsgt_hist_em, "北向资金历史", symbol="北向资金")
    if df is None or df.empty:
        return result

    recent = df.tail(10)
    result["daily"] = recent

    # 优先用 当日成交净买额
    net_col = "当日成交净买额"
    if net_col in recent.columns and recent[net_col].notna().any():
        valid = recent[net_col].dropna()
        if len(valid) >= 3:
            total = valid.tail(5).sum()
            direction = "净流入" if total > 0 else "净流出"
            result["summary"] = f"近5日北向资金{direction}{abs(total / 1e8):.2f}亿元"
            return result

    # 回退：用持股市值变化估算
    hold_col = "持股市值"
    if hold_col in recent.columns and recent[hold_col].notna().any():
        valid = recent[hold_col].dropna()
        if len(valid) >= 2:
            chg = valid.iloc[-1] - valid.iloc[-min(6, len(valid))]
            direction = "增" if chg > 0 else "减"
            result["summary"] = f"近5日持股市值变动{direction}{abs(chg / 1e8):.0f}亿（估算）"

    return result


def fetch_north_summary():
    """获取北向/南向资金当日汇总"""
    df = _safe_call(ak.stock_hsgt_fund_flow_summary_em, "北向资金汇总")
    if df is None or df.empty:
        return None, ""
    north_df = df[df["资金方向"] == "北向"] if "资金方向" in df.columns else df
    parts = []
    for _, row in north_df.iterrows():
        board = row.get("板块", "")
        amount = row.get("成交净买额", 0)
        if pd.isna(amount):
            amount = 0
        parts.append(f"{board} {amount/1e8:+.1f}亿")
    return north_df, " | ".join(parts)


def fetch_market_hot():
    """获取市场热度排名（可能因数据源限流失败）"""
    df = _safe_call(ak.stock_hot_rank_em, "市场热度排名", retries=3)
    if df is None or df.empty:
        return []
    stocks = []
    for _, row in df.head(30).iterrows():
        name = str(row.get("股票名称", ""))
        code = str(row.get("代码", ""))
        change = row.get("涨跌幅", 0)
        change_val = float(change) if pd.notna(change) else 0
        stocks.append({"code": code, "name": name, "change": change_val})
    return stocks


def fetch_market_fund_flow():
    """获取全市场资金流向（大盘资金面参考）"""
    df = _safe_call(ak.stock_market_fund_flow, "市场资金流向", retries=3)
    if df is None or df.empty:
        return None
    latest = df.iloc[-1]
    net = latest.get("主力净流入-净额", 0)
    direction = "流入" if net > 0 else "流出"
    return {
        "date": str(latest.get("日期", "")),
        "net_flow": float(net) if pd.notna(net) else 0,
        "summary": f"主力资金净{direction}{abs(net / 1e8):.1f}亿",
    }


def fetch_sector_flow():
    """获取行业板块资金流向（可能因数据源限流失败）"""
    df = _safe_call(ak.stock_sector_fund_flow_rank, "行业资金流向", retries=3, indicator="今日")
    if df is None or df.empty:
        return []
    sectors = []
    name_col = "名称" if "名称" in df.columns else df.columns[0]
    flow_col = (
        "主力净流入-净额" if "主力净流入-净额" in df.columns
        else "今日主力净流入-净额" if "今日主力净流入-净额" in df.columns
        else df.columns[1]
    )
    for _, row in df.head(20).iterrows():
        name = str(row.get(name_col, ""))
        flow = float(row.get(flow_col, 0)) if pd.notna(row.get(flow_col, "")) else 0
        sectors.append({"name": name, "net_flow": flow})
    return sectors


# ── 分析引擎 ──────────────────────────────────────────

def match_keywords(text, keywords):
    """匹配文本中的关键词并返回匹配列表"""
    return [kw for kw in keywords if kw in text]


def simple_sentiment(text):
    """简单的情感分析：正负词计数"""
    pos = sum(1 for w in POSITIVE_WORDS if w in text)
    neg = sum(1 for w in NEGATIVE_WORDS if w in text)
    if pos > neg:
        return "positive", pos - neg
    elif neg > pos:
        return "negative", neg - pos
    return "neutral", 0


def extract_companies(text):
    """从文本中提取命中的公司/行业关键词（>=3字，排除资金面通用词）"""
    companies = set()
    for sector, kws in KEYWORDS.items():
        if sector == "资金面":
            continue
        for kw in kws:
            if len(kw) >= 3 and kw in text:
                companies.add(kw)
    return companies


def analyze(news_list, hot_stocks):
    """核心分析：关键词热度 + 情感 + 市场热度交叉"""
    company_mentions = Counter()
    company_sentiment = defaultdict(int)
    keyword_hits = Counter()
    company_titles = defaultdict(list)

    for news in news_list:
        text = news["title"] + " " + news["content"]

        all_hits = []
        for sector, kws in KEYWORDS.items():
            if sector == "资金面":
                continue
            hits = match_keywords(text, kws)
            if hits:
                all_hits.extend(hits)
                keyword_hits.update(hits)

        if not all_hits:
            continue

        companies = extract_companies(text)
        _, score = simple_sentiment(text)

        for c in companies:
            company_mentions[c] += 1
            company_sentiment[c] += score
            if len(company_titles[c]) < 5:
                company_titles[c].append(news["title"])

    # 市场热度股票
    hot_names = {s["name"] for s in hot_stocks}
    hot_change = {s["name"]: s["change"] for s in hot_stocks}

    scored = []
    for name, count in company_mentions.items():
        sent = company_sentiment[name]
        in_hot = name in hot_names
        hot_bonus = 1.5 if in_hot else 1.0
        chg = hot_change.get(name, 0)
        raw_score = count * (1 + 0.3 * sent) * hot_bonus

        scored.append({
            "name": name,
            "mentions": count,
            "sentiment": round(sent, 1),
            "in_hot_rank": in_hot,
            "hot_change": round(chg, 2),
            "score": round(max(raw_score, 0), 2),
            "recent_titles": company_titles[name],
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored, keyword_hits


# ── 输出 ──────────────────────────────────────────────

def print_report(scored, keyword_hits, north_summary, north_detail, market_flow, sector_flow, top_n):
    """终端报告输出"""
    print("\n" + "=" * 80)
    print(f"  财经资讯分析报告  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 80)

    # 资金面概览
    if north_summary:
        print(f"\n  [北向资金] {north_summary}")
    if north_detail:
        print(f"  [当日汇总] {north_detail}")
    if market_flow:
        print(f"  [主力资金] {market_flow['summary']}")

    if not scored:
        print("\n  暂无符合新能源/AI关键词的新闻数据")
        return ""

    # 表格
    hdr = f"  {'排名':<4} {'公司/关键词':<16} {'热度':<6} {'情感':<6} {'涨跌%':<8} {'综合分':<8} {'热度榜'}"
    print(f"\n{hdr}")
    print("  " + "-" * 74)

    report_lines = []
    for i, item in enumerate(scored[:top_n], 1):
        hot_flag = "TOP" if item["in_hot_rank"] else "-"
        line = (
            f"  {i:<4} {item['name']:<16} {item['mentions']:<6} "
            f"{item['sentiment']:<6} {item['hot_change']:<8} {item['score']:<8} {hot_flag}"
        )
        print(line)
        report_lines.append(
            f"{i}. {item['name']} | 热度:{item['mentions']} | "
            f"情感:{item['sentiment']} | 涨跌:{item['hot_change']}% | 评分:{item['score']}"
        )

    print("  " + "-" * 74)
    print(f"\n  共分析 {len(scored)} 个关键词相关实体\n")

    # 关键词热度
    if keyword_hits:
        print("  关键词热度 TOP10:")
        for kw, cnt in keyword_hits.most_common(10):
            print(f"    {kw}: {cnt}次")
        print()

    # 行业资金流向
    if sector_flow:
        print("  行业资金流入 TOP5:")
        for s in sector_flow[:5]:
            direction = "流入" if s["net_flow"] > 0 else "流出"
            print(f"    {s['name']}: {direction}{abs(s['net_flow'] / 1e8):.2f}亿")
        print()

    # 重点公司新闻
    for item in scored[:5]:
        if item["recent_titles"]:
            print(f"  [{item['name']}] 相关新闻:")
            for t in item["recent_titles"][:3]:
                print(f"    - {t}")
            print()

    return "\n".join(report_lines)


def export_csv(scored, keyword_hits):
    """导出 CSV"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = SCRIPT_DIR / f"report_{timestamp}.csv"
    rows = []
    for item in scored:
        rows.append({
            "排名": 0, "名称": item["name"], "热度": item["mentions"],
            "情感分": item["sentiment"], "涨跌幅": item["hot_change"],
            "综合评分": item["score"], "市场热度TOP30": "Y" if item["in_hot_rank"] else "N",
        })
    for i, r in enumerate(rows, 1):
        r["排名"] = i
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    print(f"  报告已导出: {path}")
    return path


# ── 推送 ──────────────────────────────────────────────

def push_bark(title, body):
    """通过 Bark 推送到 iOS 设备"""
    key = CONFIG["bark_key"]
    if not key:
        print("  [INFO] 未配置 BARK_KEY，跳过推送")
        return False

    from urllib.parse import quote
    url = f"{CONFIG['bark_url']}/{key}/{quote(title)}/{quote(body)}"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("code") == 200:
            print(f"  推送成功: {title}")
            return True
        print(f"  [WARN] 推送失败: {data}")
        return False
    except Exception as e:
        print(f"  [WARN] 推送异常: {e}")
        return False


def build_push_message(scored, north_summary, north_detail, keyword_hits, top_n=8):
    """构建推送文本"""
    if not scored:
        return "今日无符合条件的潜力股"

    lines = [
        f"【{datetime.now().strftime('%m-%d %H:%M')}】",
        north_summary,
        north_detail,
        "",
        "潜力关注:",
    ]
    for i, item in enumerate(scored[:top_n], 1):
        hot = " TOP" if item["in_hot_rank"] else ""
        lines.append(f"{i}.{item['name']}({item['score']}){hot}")

    if keyword_hits:
        top_kw = [kw for kw, _ in keyword_hits.most_common(5)]
        lines.append("\n热门词: " + "、".join(top_kw))

    body = "\n".join(lines)
    return body[:480] + "..." if len(body) > 500 else body


# ── 主流程 ──────────────────────────────────────────────

def run_analysis():
    """执行一次完整分析"""
    t0 = time.time()
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 开始采集数据...")

    # 1. 新闻
    print("  -> 采集财经新闻...")
    all_news = fetch_news()
    print(f"     共获取 {len(all_news)} 条新闻")

    # 2. 北向资金
    print("  -> 获取北向资金数据...")
    north_flow = fetch_north_flow()
    north_df, north_detail = fetch_north_summary()
    print(f"     {north_flow['summary']}")

    # 3. 市场资金流向
    print("  -> 获取市场资金流向...")
    market_flow = fetch_market_fund_flow()
    if market_flow:
        print(f"     {market_flow['summary']}")

    # 4. 市场热度排名（可选，可能因限流失败）
    print("  -> 获取市场热度排名...")
    hot_stocks = fetch_market_hot()
    if hot_stocks:
        print(f"     获取 Top{len(hot_stocks)} 热度股")
    else:
        print(f"     (跳过：数据源暂时不可用)")

    # 5. 行业资金流向（可选）
    print("  -> 获取行业资金流向...")
    sector_flow = fetch_sector_flow()
    if sector_flow:
        print(f"     获取 {len(sector_flow)} 个行业数据")
    else:
        print(f"     (跳过：数据源暂时不可用)")

    # 6. 分析
    print("  -> 分析中...")
    scored, keyword_hits = analyze(all_news, hot_stocks)

    # 7. 输出
    report = print_report(scored, keyword_hits, north_flow["summary"], north_detail, market_flow, sector_flow, CONFIG["top_n"])

    # 8. 导出 CSV
    export_csv(scored, keyword_hits)

    # 9. 推送
    if CONFIG["bark_key"]:
        title = "财经分析日报"
        body = build_push_message(scored, north_flow["summary"], north_detail, keyword_hits)
        push_bark(title, body)

    elapsed = time.time() - t0
    print(f"  分析完成，耗时 {elapsed:.1f}s\n")

    return scored


def run_schedule():
    """定时调度模式"""
    schedule_time = CONFIG["schedule_time"]
    hour, minute = map(int, schedule_time.split(":"))

    print(f"定时模式启动，每日 {schedule_time} 执行分析")
    print(f"按 Ctrl+C 停止\n")

    run_analysis()

    while True:
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        print(f"[{now:%H:%M:%S}] 下次执行: {target:%Y-%m-%d %H:%M:%S} ({wait / 60:.0f}分钟后)")

        time.sleep(wait)
        try:
            run_analysis()
        except Exception as e:
            print(f"[ERROR] 分析异常: {e}")
            time.sleep(60)


# ── 入口 ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="财经资讯分析 & 潜力股挖掘")
    parser.add_argument("--schedule", action="store_true", help="定时模式，每日收盘后执行")
    parser.add_argument("--top", type=int, default=CONFIG["top_n"], help=f"展示 Top N")
    parser.add_argument("--no-push", action="store_true", help="禁用 Bark 推送")
    args = parser.parse_args()

    if args.top != CONFIG["top_n"]:
        CONFIG["top_n"] = args.top
    if args.no_push:
        CONFIG["bark_key"] = ""

    if args.schedule:
        run_schedule()
    else:
        run_analysis()


if __name__ == "__main__":
    main()
